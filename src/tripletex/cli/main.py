"""Tripletex CLI — main entry point."""

from __future__ import annotations

import asyncio
import logging
import sys

import click

from tripletex.config import load_config


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
    """Async context manager that switches company context after authenticating."""

    def __init__(self, ctx):
        self._ctx = ctx
        self._client = _make_client(ctx)
        self._company_cm = None

    async def __aenter__(self):
        client = await self._client.__aenter__()
        company_name = self._ctx.obj.get("company_name")
        if company_name:
            from tripletex.session import WebSession
            if not isinstance(client.session, WebSession):
                raise click.ClickException("--company requires web auth (use --auth web)")
            companies = await client.list_companies()
            match = [c for c in companies if company_name.lower() in c.display_name.lower()]
            if not match:
                names = ", ".join(c.display_name for c in companies)
                raise click.ClickException(f"Company '{company_name}' not found. Available: {names}")
            self._company_cm = client.company_context(match[0])
            await self._company_cm.__aenter__()
        return client

    async def __aexit__(self, *exc):
        if self._company_cm:
            await self._company_cm.__aexit__(*exc)
        await self._client.__aexit__(*exc)


def _client(ctx):
    """Create a client, optionally switching to --company."""
    if ctx.obj.get("company_name"):
        return _CompanyClientWrapper(ctx)
    return _make_client(ctx)


def run_async(coro):
    """Run an async function from a sync Click command."""
    return asyncio.run(coro)


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
@click.option("--status", default="FOR_APPROVAL", help="Status filter: FOR_APPROVAL, APPROVED, SENT_TO_BANK, ALL, or comma-separated")
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

        _ALL_STATUSES = "CANCELLED,REJECTED_BY_THE_BANK,FOR_APPROVAL,UNDER_PROCESSING"
        status_value = _ALL_STATUSES if status.upper() == "ALL" else status

        output_lines: list[str] = []

        async with _client(ctx) as client:
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
    """Voucher backup commands."""
    pass


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
@click.option("--from", "from_date", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--to", "to_date", required=True, help="End date (YYYY-MM-DD)")
@click.pass_context
def order_list(ctx, from_date, to_date):
    """List orders in a date range."""
    from datetime import date as date_cls

    from tripletex.endpoints.orders import list_orders

    async def _list():
        async with _client(ctx) as client:
            orders = await list_orders(
                client,
                date_cls.fromisoformat(from_date),
                date_cls.fromisoformat(to_date),
            )
            for o in orders:
                cust = o.customer.get("displayName", "") if o.customer else ""
                click.echo(f"{o.id}\t{o.number or ''}\t{o.order_date}\t{cust}")

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
@click.option("--from", "from_date", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--to", "to_date", required=True, help="End date (YYYY-MM-DD)")
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
            for inv in invoices:
                click.echo(f"{inv.id}\t{inv.invoice_number or ''}\t{inv.invoice_date}\t{inv.amount_currency or ''}")

    run_async(_list())


cli.add_command(reconciliation)
cli.add_command(payments)
cli.add_command(vouchers)
cli.add_command(wages)
cli.add_command(customer)
cli.add_command(product)
cli.add_command(order)
cli.add_command(invoice)
