"""Tripletex CLI — main entry point."""

from __future__ import annotations

import asyncio
import logging
import sys

import click

from tripletex.config import load_config
from tripletex.session import AuthUnavailable, require_web_session


@click.group()
@click.option("--config", "config_path", type=click.Path(), default=None, help="Config file path")
@click.option("--env", "env_name", default=None, help="Config section name (default: 'default')")
@click.option("--auth", "auth_mode", type=click.Choice(["web", "api", "auto"]), default="auto", help="Auth mode: web (Visma Connect), api (token), auto (detect)")
@click.option("--company", "company_name", default=None, help="Switch to a specific company (by name)")
@click.option("--cookie", envvar="TRIPLETEX_COOKIE", default=None, help="Browser cookie string")
@click.option("--context-id", envvar="TRIPLETEX_CONTEXT_ID", default=None, help="Tripletex context ID")
@click.option("--csrf-token", envvar="TRIPLETEX_CSRF_TOKEN", default=None, help="CSRF token")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx, config_path, env_name, auth_mode, company_name, cookie, context_id, csrf_token, verbose):
    """Tripletex CLI — bank reconciliation, payments, voucher backup, and more."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(
        config_path=config_path,
        env_name=env_name,
        cookie=cookie,
        context_id=context_id,
        csrf_token=csrf_token,
    )
    ctx.obj["auth_mode"] = auth_mode
    ctx.obj["company_name"] = company_name


def _make_client(ctx):
    """Create a TripletexClient with the right auth mode."""
    from tripletex.client import TripletexClient

    config = ctx.obj["config"]
    auth_mode = ctx.obj.get("auth_mode", "auto")
    if auth_mode == "web":
        return TripletexClient.web(config)
    elif auth_mode == "api":
        return TripletexClient.api(config)
    else:
        return TripletexClient(config)


class _CompanyClientWrapper:
    """Async context manager that binds the client to --company after authenticating."""

    def __init__(self, ctx):
        self._ctx = ctx
        self._client = _make_client(ctx)

    async def __aenter__(self):
        client = await self._client.__aenter__()
        company_name = self._ctx.obj.get("company_name")
        if not company_name:
            return client

        require_web_session(client.session, "--company")
        companies = await client.list_companies()
        match = [c for c in companies if company_name.lower() in c.display_name.lower()]
        if not match:
            names = ", ".join(c.display_name for c in companies)
            raise click.ClickException(f"Company '{company_name}' not found. Available: {names}")
        # Hand back a sibling bound to the company. The client we authenticated
        # stays the owner of the connection pool and is what __aexit__ closes.
        return client.for_company(match[0])

    async def __aexit__(self, *exc):
        await self._client.__aexit__(*exc)


def _client(ctx):
    """Create a client, optionally switching to --company."""
    if ctx.obj.get("company_name"):
        return _CompanyClientWrapper(ctx)
    return _make_client(ctx)


def run_async(coro):
    """Run an async function from a sync Click command.

    Turns the auth problems users routinely hit — asking for a web-only feature
    under API-token auth, or an expired web session with no terminal to re-login
    from — into plain CLI messages instead of tracebacks.
    """
    try:
        return asyncio.run(coro)
    except AuthUnavailable as e:
        raise click.ClickException(str(e)) from e


# --- Login ---


@cli.command()
@click.pass_context
def login(ctx):
    """Interactive Visma Connect login. Persists session to ~/.tripletex/."""
    from tripletex.client import TripletexClient

    async def _login():
        config = ctx.obj["config"]
        client = TripletexClient.web(config)
        await client.authenticate()
        click.echo(f"Logged in. Context ID: {client.session.context_id}")
        name = config.env_name or "default"
        click.echo(f"Session saved to {config.session_dir / f'session_{name}.json'}")
        await client.close()

    run_async(_login())


# --- Companies ---


@cli.command()
@click.pass_context
def companies(ctx):
    """List accessible companies (web auth only)."""
    async def _companies():
        async with _client(ctx) as client:
            comps = await client.list_companies()
            for c in comps:
                click.echo(f"{c.id}\t{c.display_name}")

    run_async(_companies())


# --- Reconciliation ---


@cli.group()
def reconciliation():
    """Bank reconciliation commands."""
    pass


@reconciliation.command("unreconciled")
@click.option("--month", required=True, help="Month in YYYY-MM format")
@click.option("--company", default=None, help="Filter to one company name")
@click.pass_context
def reconciliation_unreconciled(ctx, month, company):
    """List unreconciled bank transactions."""
    from datetime import date as date_cls
    import calendar

    from tripletex.endpoints.reconciliation import get_unreconciled_transactions

    async def _unreconciled():
        year, mon = map(int, month.split("-"))
        start = date_cls(year, mon, 1)
        end = date_cls(year, mon, calendar.monthrange(year, mon)[1])

        async with _client(ctx) as client:
            require_web_session(client.session, "Reconciliation across companies")
            async for comp, comp_client in client.iter_companies():
                if company and company.lower() not in comp.display_name.lower():
                    continue
                results = await get_unreconciled_transactions(comp_client, start, end)
                for account, txns in results:
                    click.echo(
                        f"# {len(txns):2d} unreconciled transactions for "
                        f"{comp.display_name} between {start} and {end}"
                    )
                    for t in txns:
                        click.echo(
                            f"{comp.display_name}\t{account.iban or account.number}\t"
                            f"{t.posted_date}\t{t.amount_currency}\t{t.details or t.description}"
                        )

    run_async(_unreconciled())



# --- Payments ---


@cli.group()
def payments():
    """Payment approval commands."""
    pass


@payments.command("list")
@click.option("--due-within", default=None, type=int, help="Only show payments due within N days")
@click.option("--status", default="FOR_APPROVAL", help="FOR_APPROVAL, UNDER_PROCESSING, CANCELLED, REJECTED_BY_THE_BANK, PAID, ALL (every status but PAID), or comma-separated")
@click.option("--company", default=None, help="Filter to one company name")
@click.option("--notify-slack", is_flag=True, help="Send results to Slack webhook")
@click.pass_context
def payments_list(ctx, due_within, status, company, notify_slack):
    """List bank payments (default: awaiting approval)."""
    from datetime import date as date_cls, timedelta

    from tripletex.endpoints.payments import list_payments

    async def _payments():
        config = ctx.obj["config"]
        limit = date_cls.today() + timedelta(days=due_within) if due_within is not None else None

        from tripletex.endpoints.payments import (
            OPEN_PAYMENT_STATUSES,
            validate_status_filter,
        )

        status_value = (
            ",".join(OPEN_PAYMENT_STATUSES) if status.upper() == "ALL" else status.upper()
        )
        try:
            validate_status_filter(status_value)
        except ValueError as e:
            raise click.ClickException(str(e)) from e

        output_lines: list[str] = []

        async with _client(ctx) as client:
            require_web_session(client.session, "Listing bank payments")
            async for comp, comp_client in client.iter_companies():
                if company and company.lower() not in comp.display_name.lower():
                    continue
                pmts = await list_payments(comp_client, status_filter=status_value)
                if limit:
                    pmts = [p for p in pmts if p.payment_date and p.payment_date <= limit]
                if pmts:
                    header = f"Payments for {comp.display_name} ({status_value}):"
                    output_lines.append(header)
                    for p in pmts:
                        line = (
                            f"  {p.payment_date}\t{p.amount_currency}\t"
                            f"{p.voucher_number}\t{p.account_number}\t"
                            f"{p.receiver_reference or p.kid or ''}"
                        )
                        output_lines.append(line)
                    output_lines.append("")

        if not output_lines:
            click.echo(f"No payments with status {status_value}")
            return

        output = "\n".join(output_lines)
        click.echo(output)

        if notify_slack and config.slack_webhook_url:
            import httpx as httpx_lib

            async with httpx_lib.AsyncClient() as http:
                await http.post(
                    config.slack_webhook_url,
                    json={
                        "username": "tripletex-helper",
                        "icon_emoji": ":ghost:",
                        "text": output,
                    },
                )
            click.echo("(Sent to Slack)")

    run_async(_payments())


# --- Vouchers ---


@cli.group()
def vouchers():
    """Voucher listing and backup commands."""
    pass


def _vouchers_json(voucher_list) -> str:
    import json

    return json.dumps([v.model_dump(mode="json") for v in voucher_list], indent=2)


@vouchers.command("list")
@click.option("--from", "from_date", required=True, help="Start date, inclusive (YYYY-MM-DD)")
@click.option("--to", "to_date", required=True, help="End date, exclusive (YYYY-MM-DD)")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of columns")
@click.pass_context
def vouchers_list(ctx, from_date, to_date, as_json):
    """List ledger vouchers in a date range (works with API tokens)."""
    from datetime import date as date_cls

    from tripletex.endpoints.vouchers import list_vouchers

    async def _list():
        async with _client(ctx) as client:
            voucher_list = await list_vouchers(
                client,
                date_cls.fromisoformat(from_date),
                date_cls.fromisoformat(to_date),
            )
            if as_json:
                click.echo(_vouchers_json(voucher_list))
                return
            for v in voucher_list:
                docs = str(len(v.document_ids)) if v.document_ids else ""
                click.echo(
                    f"{v.id}\t{v.display_number}\t{v.date or ''}\t{docs}\t{v.description or ''}"
                )

    run_async(_list())


@vouchers.command("queue")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of columns")
@click.pass_context
def vouchers_queue(ctx, as_json):
    """List vouchers registered but not yet posted (works with API tokens).

    This is the "to process" backlog. The voucher inbox is a different queue and
    needs web auth.
    """
    from datetime import date

    from tripletex.endpoints.vouchers import list_non_posted_vouchers

    async def _queue():
        async with _client(ctx) as client:
            voucher_list = await list_non_posted_vouchers(client)
            if as_json:
                click.echo(_vouchers_json(voucher_list))
                return
            for v in sorted(voucher_list, key=lambda v: (v.date or date.min, v.id)):
                click.echo(
                    f"{v.id}\t{v.date or ''}\t{v.display_number}\t{v.description or ''}"
                )
            click.echo(f"\n{len(voucher_list)} vouchers waiting to be processed")

    run_async(_queue())


@vouchers.command("reception")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of columns")
@click.pass_context
def vouchers_reception(ctx, as_json):
    """List documents in voucher reception — bilagsmottak (works with API tokens).

    Same rows as the web-only voucher inbox, without its triage metadata
    (arrival timestamp, amount, supplier, channel).
    """
    from datetime import date

    from tripletex.endpoints.vouchers import list_reception_vouchers

    async def _reception():
        async with _client(ctx) as client:
            voucher_list = await list_reception_vouchers(client)
            if as_json:
                click.echo(_vouchers_json(voucher_list))
                return
            for v in sorted(voucher_list, key=lambda v: (v.date or date.min, v.id)):
                docs = str(len(v.document_ids)) if v.document_ids else ""
                click.echo(
                    f"{v.id}\t{v.date or ''}\t{v.display_number}\t{docs}\t{v.description or ''}"
                )
            click.echo(f"\n{len(voucher_list)} documents in reception")

    run_async(_reception())


@cli.group()
def documents():
    """Documents reception — files waiting for an employee."""


@documents.command("queue")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of columns")
@click.pass_context
def documents_queue(ctx, as_json):
    """List files waiting in documents reception (works with API tokens).

    A different queue from `vouchers reception`: these are files mailed or
    uploaded to an employee's document inbox, with no voucher or amount
    attached. Ages are in whole days — Tripletex records only a date here.
    """
    import json as _json
    from datetime import date

    from tripletex.endpoints.documents import (
        get_document_reception_context,
        list_document_reception,
    )

    async def _queue():
        async with _client(ctx) as client:
            items = await list_document_reception(client)
            context = await get_document_reception_context(client)

            if as_json:
                click.echo(
                    _json.dumps(
                        {
                            "documents": [i.model_dump(mode="json") for i in items],
                            "context": context.model_dump(mode="json"),
                        },
                        indent=2,
                    )
                )
                return

            for i in sorted(items, key=lambda i: (i.created or date.min, i.document_id)):
                age = "" if i.age_days is None else f"{i.age_days}d"
                click.echo(
                    f"{i.document_id}\t{i.created or ''}\t{age}\t"
                    f"{i.receiver_name or ''}\t{i.display_size or ''}\t"
                    f"{i.document_name or ''}"
                )

            click.echo(f"\n{len(items)} documents waiting")
            if not context.auth_all_employees:
                # Otherwise an empty queue reads as "nothing to do" when it may
                # only mean "nothing addressed to me".
                click.echo(
                    "Note: this token sees only its own employee's documents "
                    "(authAllEmployees is false), so the count above is partial."
                )
            if context.document_reception_email:
                click.echo(f"Send documents to: {context.document_reception_email}")

    run_async(_queue())


@documents.command("get")
@click.argument("document_id", type=int)
@click.option("--out", "dest", type=click.Path(), default=None, help="Destination path")
@click.pass_context
def documents_get(ctx, document_id, dest):
    """Download one document's contents by id (works with API tokens)."""
    from pathlib import Path

    from tripletex.endpoints.documents import download_document

    async def _get():
        async with _client(ctx) as client:
            meta = await client.get_json(f"/v2/document/{document_id}")
            name = meta["value"].get("fileName") or f"document_{document_id}"
            path = Path(dest) if dest else Path(name)
            await download_document(client, document_id, path)
            click.echo(f"{path}  ({path.stat().st_size} bytes)")

    run_async(_get())


@vouchers.command("backup")
@click.option("--output-dir", required=True, type=click.Path(), help="Destination directory")
@click.option("--from", "from_date", default=None, help="Start date (YYYY-MM-DD)")
@click.option("--to", "to_date", default=None, help="End date (YYYY-MM-DD)")
@click.option("--company", default=None, help="Filter to one company name")
@click.pass_context
def vouchers_backup(ctx, output_dir, from_date, to_date, company):
    """Download all vouchers with metadata and documents."""
    from datetime import date as date_cls
    from pathlib import Path

    from tripletex.endpoints.vouchers import backup_all_vouchers

    async def _backup():
        d_from = date_cls.fromisoformat(from_date) if from_date else None
        d_to = date_cls.fromisoformat(to_date) if to_date else None

        async with _client(ctx) as client:
            async for comp, comp_client in client.iter_companies():
                if company and company.lower() not in comp.display_name.lower():
                    continue
                comp_dir = Path(output_dir) / comp.display_name.replace(" ", "_")
                click.echo(f"Backing up vouchers for {comp.display_name}...")
                voucher_list = await backup_all_vouchers(
                    comp_client, comp_dir, d_from, d_to
                )
                click.echo(f"  Done: {len(voucher_list)} vouchers")

    run_async(_backup())


# --- Inbox ---


@cli.command("inbox")
@click.option("--company", default=None, help="Filter to one company name")
@click.pass_context
def inbox(ctx, company):
    """List unprocessed items in the voucher inbox."""
    from tripletex.endpoints.inbox import list_inbox

    async def _inbox():
        async with _client(ctx) as client:
            require_web_session(client.session, "The voucher inbox")
            async for comp, comp_client in client.iter_companies():
                if company and company.lower() not in comp.display_name.lower():
                    continue
                items = await list_inbox(comp_client)
                if items:
                    click.echo(f"Inbox for {comp.display_name} ({len(items)} items):")
                    for item in items:
                        amt = f"{item.invoice_amount} {item.invoice_currency}" if item.invoice_amount else ""
                        click.echo(
                            f"  {item.received_date.strftime('%Y-%m-%d') if item.received_date else ''}\t"
                            f"{amt:>15}\t{item.filter_type or ''}\t"
                            f"{item.description or item.filename or ''}"
                        )
                    click.echo()

    run_async(_inbox())


# --- Wages ---


@cli.group()
def wages():
    """Employee wage commands."""
    pass


@wages.command("dump")
@click.option("--output", "-o", default=None, type=click.Path(), help="Output JSON file")
@click.pass_context
def wages_dump(ctx, output):
    """Dump employee salary data to JSON."""
    import json
    from pathlib import Path

    from tripletex.endpoints.wages import fetch_all_wages

    async def _dump():
        async with _client(ctx) as client:
            data = await fetch_all_wages(client)

            if output:
                Path(output).write_text(json.dumps(data, indent=2, default=str))
                click.echo(f"Saved {len(data['employees'])} employees to {output}")
            else:
                click.echo(json.dumps(data, indent=2, default=str))

    run_async(_dump())


# --- Employees (API) ---


def _employment_summary(employee, on):
    """One-line employment status, e.g. 'active since 2026-05-01 (DLC)'."""
    active = employee.active_employments(on)
    if active:
        e = active[0]
        return f"active since {e.start_date or '?'}\t{e.division_name}"
    last = employee.latest_employment
    if last is None:
        return "no employment\t"
    reason = f" ({last.end_reason})" if last.end_reason else ""
    return f"ended {last.end_date or '?'}{reason}\t{last.division_name}"


@cli.group()
def employee():
    """Employee commands."""
    pass


@employee.command("list")
@click.option("--query", "-q", default=None, help="Search query")
@click.option("--active", is_flag=True, help="Only employees with an active employment")
@click.pass_context
def employee_list(ctx, query, active):
    """List employees with their employment status."""
    from datetime import date

    from tripletex.endpoints.employees import list_employees

    async def _list():
        async with _client(ctx) as client:
            today = date.today()
            employees = await list_employees(client, query=query)
            for e in employees:
                if active and not e.has_active_employment(today):
                    continue
                click.echo(
                    f"{e.id}\t{e.employee_number or ''}\t{e.display_name}\t"
                    f"{e.department_name}\t{_employment_summary(e, today)}"
                )

    run_async(_list())


@employee.command("get")
@click.argument("employee_id", type=int)
@click.pass_context
def employee_get(ctx, employee_id):
    """Get an employee by ID."""
    from tripletex.endpoints.employees import get_employee

    async def _get():
        async with _client(ctx) as client:
            e = await get_employee(client, employee_id)
            click.echo(e.model_dump_json(indent=2))

    run_async(_get())


@employee.command("access")
@click.option("--all", "show_all", is_flag=True, help="Show every employee, not just problems")
@click.option(
    "--include-inactive",
    is_flag=True,
    help="Also check employees without an active employment (slower)",
)
@click.pass_context
def employee_access(ctx, show_all, include_inactive):
    """Check login access against employment status (web auth).

    Flags employees who have an active employment but can no longer log in —
    what happens when an employment is ended by a unit change and the new one
    does not restore access.
    """
    from datetime import date

    from tripletex.endpoints.employees import (
        fetch_access_report,
        find_access_issues,
        list_employees,
    )

    async def _access():
        async with _client(ctx) as client:
            require_web_session(client.session, "Checking employee login access")
            today = date.today()
            employees = await list_employees(client)
            if not include_inactive:
                employees = [e for e in employees if e.has_active_employment(today)]

            report = await fetch_access_report(client, employees)
            issues = {e.id for e, _ in find_access_issues(report, today)}

            for e, access in report:
                if not show_all and e.id not in issues:
                    continue
                flag = "  <-- ACCESS ENDED" if e.id in issues else ""
                click.echo(
                    f"{e.id}\t{e.employee_number or ''}\t{e.display_name}\t"
                    f"{_employment_summary(e, today)}\t"
                    f"login={'yes' if access.allow_login else 'no'}\t"
                    f"until={access.login_end_date or ''}{flag}"
                )

            click.echo(
                f"\n{len(issues)} of {len(report)} checked employees have an active "
                f"employment but no login access."
            )

    run_async(_access())


@employee.command("payslip")
@click.option("--manual", is_flag=True, help="Only employees whose payslips are handled manually")
@click.option("--include-resigned", is_flag=True, help="Also include resigned employees")
@click.pass_context
def employee_payslip(ctx, manual, include_resigned):
    """Show how each employee's payslip is delivered (web auth).

    The delivery method is a display string localized to your Tripletex language,
    e.g. "The Tripletex app" or "Manual handling".
    """
    from collections import Counter

    from tripletex.endpoints.employees import fetch_employee_overview

    async def _payslip():
        async with _client(ctx) as client:
            rows = await fetch_employee_overview(client)
            if not include_resigned:
                rows = [r for r in rows if not r.has_resigned]

            counts = Counter(r.payslip_delivery or "(unknown)" for r in rows)

            for r in rows:
                if manual and r.payslip_via_app:
                    continue
                click.echo(
                    f"{r.id}\t{r.employee_number or ''}\t{r.display_name}\t"
                    f"{r.payslip_delivery or ''}"
                )

            click.echo("")
            for method, n in counts.most_common():
                click.echo(f"{n}\t{method}")

    run_async(_payslip())


# --- Customers (API) ---


@cli.group()
def customer():
    """Customer commands (API auth)."""
    pass


@customer.command("list")
@click.option("--query", "-q", default=None, help="Search query")
@click.pass_context
def customer_list(ctx, query):
    """List customers."""
    from tripletex.endpoints.customers import list_customers

    async def _list():
        async with _client(ctx) as client:
            customers = await list_customers(client, query=query)
            for c in customers:
                click.echo(f"{c.id}\t{c.customer_number or ''}\t{c.name}\t{c.email or ''}")

    run_async(_list())


@customer.command("get")
@click.argument("customer_id", type=int)
@click.pass_context
def customer_get(ctx, customer_id):
    """Get a customer by ID."""
    import json

    from tripletex.endpoints.customers import get_customer

    async def _get():
        async with _client(ctx) as client:
            c = await get_customer(client, customer_id)
            click.echo(c.model_dump_json(indent=2))

    run_async(_get())


# --- Products (API) ---


@cli.group()
def product():
    """Product commands (API auth)."""
    pass


@product.command("list")
@click.option("--query", "-q", default=None, help="Search query")
@click.pass_context
def product_list(ctx, query):
    """List products."""
    from tripletex.endpoints.products import list_products

    async def _list():
        async with _client(ctx) as client:
            products = await list_products(client, query=query)
            for p in products:
                click.echo(f"{p.id}\t{p.number or ''}\t{p.name}\t{p.price_excluding_vat_currency or ''}")

    run_async(_list())


# --- Orders (API) ---


@cli.group()
def order():
    """Order commands (API auth)."""
    pass


@order.command("list")
@click.option("--from", "from_date", required=True, help="Start date, inclusive (YYYY-MM-DD)")
@click.option("--to", "to_date", required=True, help="End date, exclusive (YYYY-MM-DD)")
@click.pass_context
def order_list(ctx, from_date, to_date):
    """List orders in a date range."""
    from datetime import date as date_cls

    from tripletex.endpoints.invoices import list_invoices_for_order
    from tripletex.endpoints.orders import list_orders

    async def _list():
        async with _client(ctx) as client:
            orders = await list_orders(
                client,
                date_cls.fromisoformat(from_date),
                date_cls.fromisoformat(to_date),
            )

            # Orders carry no link to their invoice(s), so we look them up via
            # /v2/invoice/{orderId}/invoices. A single invoice can bundle many
            # orders (and that lookup returns the invoice's full order list), so
            # cache invoice numbers per order id: once an order is seen — either
            # queried directly or bundled into another order's invoice — we skip
            # the redundant call. (A 7-order bundle then costs one call, not 7.)
            inv_by_order: dict[int, list[str]] = {}

            for o in orders:
                if o.id in inv_by_order:
                    continue
                invoices = await list_invoices_for_order(
                    client, o.id, fields="invoiceNumber,orders(id)"
                )
                inv_by_order.setdefault(o.id, [])
                for inv in invoices:
                    if not inv.invoice_number:
                        continue
                    num = str(inv.invoice_number)
                    for ref in inv.orders or []:
                        oid = ref.get("id")
                        if oid is None:
                            continue
                        nums = inv_by_order.setdefault(oid, [])
                        if num not in nums:
                            nums.append(num)

            click.echo(
                "ID\tNUMBER\tREFERENCE\tORDER DATE\tDELIVERY\tCUSTOMER\tSTATUS\t"
                "INVOICE NO\tAMOUNT INC VAT\tAMOUNT EXC VAT"
            )
            for o in orders:
                status = "Closed" if o.is_closed else "Open"
                inc = o.amount_including_vat
                exc = o.amount_excluding_vat
                inv_no = ", ".join(inv_by_order.get(o.id, []))
                click.echo(
                    f"{o.id}\t{o.number or ''}\t{o.reference or ''}\t"
                    f"{o.order_date or ''}\t{o.delivery_date or ''}\t"
                    f"{o.customer_name}\t{status}\t{inv_no}\t"
                    f"{inc if inc is not None else ''}\t"
                    f"{exc if exc is not None else ''}"
                )

    run_async(_list())


@order.command("get")
@click.argument("order_id", type=int)
@click.pass_context
def order_get(ctx, order_id):
    """Get an order by ID."""
    from tripletex.endpoints.orders import get_order

    async def _get():
        async with _client(ctx) as client:
            o = await get_order(client, order_id)
            click.echo(o.model_dump_json(indent=2))

    run_async(_get())


# --- Invoices (API) ---


@cli.group()
def invoice():
    """Invoice commands (API auth)."""
    pass


@invoice.command("list")
@click.option("--from", "from_date", required=True, help="Start date, inclusive (YYYY-MM-DD)")
@click.option("--to", "to_date", required=True, help="End date, exclusive (YYYY-MM-DD)")
@click.pass_context
def invoice_list(ctx, from_date, to_date):
    """List invoices in a date range."""
    from datetime import date as date_cls

    from tripletex.endpoints.invoices import list_invoices

    async def _list():
        async with _client(ctx) as client:
            invoices = await list_invoices(
                client,
                date_cls.fromisoformat(from_date),
                date_cls.fromisoformat(to_date),
            )
            click.echo(
                "ID\tNO\tCUSTOMER\tREFERENCE\tSTATUS\tINVOICE DATE\tDUE\t"
                "AMOUNT INC VAT\tCUR\tAMOUNT EXC VAT\tOUTSTANDING"
            )
            for inv in invoices:
                click.echo(
                    f"{inv.id}\t{inv.invoice_number or ''}\t{inv.customer_name}\t"
                    f"{inv.reference}\t{inv.status}\t{inv.invoice_date or ''}\t"
                    f"{inv.due_date or ''}\t{inv.amount_currency or ''}\t"
                    f"{inv.currency_code}\t{inv.amount_excluding_vat or ''}\t"
                    f"{inv.amount_outstanding or ''}"
                )

    run_async(_list())


cli.add_command(reconciliation)
cli.add_command(payments)
cli.add_command(vouchers)
cli.add_command(wages)
cli.add_command(customer)
cli.add_command(product)
cli.add_command(order)
cli.add_command(invoice)
