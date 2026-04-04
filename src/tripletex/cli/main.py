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
@click.option("--cookie", envvar="TRIPLETEX_COOKIE", default=None, help="Browser cookie string")
@click.option("--context-id", envvar="TRIPLETEX_CONTEXT_ID", default=None, help="Tripletex context ID")
@click.option("--csrf-token", envvar="TRIPLETEX_CSRF_TOKEN", default=None, help="CSRF token")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx, config_path, env_name, cookie, context_id, csrf_token, verbose):
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
        client = TripletexClient(config)
        await client.authenticate()
        click.echo(f"Logged in. Context ID: {client.session.context_id}")
        click.echo(f"Session saved to {config.session_dir / 'session.json'}")
        await client.close()

    run_async(_login())


# --- Companies ---


@cli.command()
@click.pass_context
def companies(ctx):
    """List accessible companies."""
    from tripletex.client import TripletexClient

    async def _companies():
        async with TripletexClient(ctx.obj["config"]) as client:
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

    from tripletex.client import TripletexClient
    from tripletex.endpoints.reconciliation import get_unreconciled_transactions

    async def _unreconciled():
        year, mon = map(int, month.split("-"))
        start = date_cls(year, mon, 1)
        end = date_cls(year, mon, calendar.monthrange(year, mon)[1])

        async with TripletexClient(ctx.obj["config"]) as client:
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


@reconciliation.command("missing-receipts")
@click.option("--month", required=True, help="Month in YYYY-MM format")
@click.option("--company", default=None, help="Filter to one company name")
@click.pass_context
def reconciliation_missing_receipts(ctx, month, company):
    """List bank transactions with no matching voucher (missing receipts)."""
    from datetime import date as date_cls
    import calendar

    from tripletex.client import TripletexClient
    from tripletex.endpoints.reconciliation import get_unreconciled_transactions

    async def _missing():
        year, mon = map(int, month.split("-"))
        start = date_cls(year, mon, 1)
        end = date_cls(year, mon, calendar.monthrange(year, mon)[1])

        async with TripletexClient(ctx.obj["config"]) as client:
            async for comp, comp_client in client.iter_companies():
                if company and company.lower() not in comp.display_name.lower():
                    continue
                results = await get_unreconciled_transactions(comp_client, start, end)
                for account, txns in results:
                    if not txns:
                        continue
                    click.echo(f"\nMissing receipts for {comp.display_name} — {account.iban or account.number}:")
                    for t in txns:
                        click.echo(
                            f"  {t.posted_date}\t{t.amount_currency:>12}\t{t.details or t.description}"
                        )

    run_async(_missing())


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

    from tripletex.client import TripletexClient
    from tripletex.endpoints.payments import list_payments

    async def _payments():
        config = ctx.obj["config"]
        limit = date_cls.today() + timedelta(days=due_within) if due_within is not None else None

        _ALL_STATUSES = "CANCELLED,REJECTED_BY_THE_BANK,FOR_APPROVAL,UNDER_PROCESSING"
        status_value = _ALL_STATUSES if status.upper() == "ALL" else status

        output_lines: list[str] = []

        async with TripletexClient(config) as client:
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

    from tripletex.client import TripletexClient
    from tripletex.endpoints.vouchers import backup_all_vouchers

    async def _backup():
        d_from = date_cls.fromisoformat(from_date) if from_date else None
        d_to = date_cls.fromisoformat(to_date) if to_date else None

        async with TripletexClient(ctx.obj["config"]) as client:
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
    from tripletex.client import TripletexClient
    from tripletex.endpoints.inbox import list_inbox

    async def _inbox():
        async with TripletexClient(ctx.obj["config"]) as client:
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

    from tripletex.client import TripletexClient
    from tripletex.endpoints.wages import fetch_all_wages

    async def _dump():
        async with TripletexClient(ctx.obj["config"]) as client:
            data = await fetch_all_wages(client)

            if output:
                Path(output).write_text(json.dumps(data, indent=2, default=str))
                click.echo(f"Saved {len(data['employees'])} employees to {output}")
            else:
                click.echo(json.dumps(data, indent=2, default=str))

    run_async(_dump())


cli.add_command(reconciliation)
cli.add_command(payments)
cli.add_command(vouchers)
cli.add_command(wages)
