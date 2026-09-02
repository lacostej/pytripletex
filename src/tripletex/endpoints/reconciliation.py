"""Bank reconciliation endpoints: accounts, periods, unreconciled transactions."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from tripletex.models import AccountingPeriod, BankAccount, BankTransaction, Reconciliation

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def list_bank_accounts(client: TripletexClient) -> list[BankAccount]:
    """List active bank accounts that require reconciliation.

    GET /v2/ledger/account?isInactive=false&isBankAccount=true
    """
    data = await client.get_json(
        "/v2/ledger/account",
        params={"isInactive": "false", "isBankAccount": "true"},
    )
    return [BankAccount.model_validate(a) for a in data.get("values", [])]


async def get_periods(
    client: TripletexClient,
    start_from: date,
    start_to: date,
) -> list[AccountingPeriod]:
    """Get accounting periods for a date range.

    GET /v2/ledger/accountingPeriod?startFrom=X&startTo=Y
    """
    data = await client.get_json(
        "/v2/ledger/accountingPeriod",
        params={
            "startFrom": start_from.isoformat(),
            "startTo": start_to.isoformat(),
        },
    )
    return [AccountingPeriod.model_validate(p) for p in data.get("values", [])]


_RECONCILIATION_FIELDS = (
    "id,bankAccountClosingBalanceCurrency,isClosed,closedDate,type,"
    "approvable,autoPayReconciliation,"
    "account(id),"
    "closedByEmployee(firstName,lastName),"
    "closedByContact(firstName,lastName),"
    "transactions(id,postedDate,amountCurrency,description),"
    "voucher(id,number,year,postings(id,row))"
)


async def get_reconciliation(
    client: TripletexClient,
    period_id: int,
    account_id: int,
) -> Reconciliation | None:
    """Get bank reconciliation for a period and account.

    GET /v2/bank/reconciliation?accountingPeriodId=X&accountId=Y&fields=...
    """
    data = await client.get_json(
        "/v2/bank/reconciliation",
        params={
            "accountingPeriodId": str(period_id),
            "accountId": str(account_id),
            "fields": _RECONCILIATION_FIELDS,
        },
    )
    values = data.get("values", [])
    if not values:
        return None
    return Reconciliation.model_validate(values[0])


async def get_approved_match_transaction_ids(
    client: TripletexClient,
    reconciliation_id: int,
) -> set[int]:
    """Get IDs of transactions that have been approved/matched.

    GET /v2/bank/reconciliation/match?bankReconciliationId=X&approved=true
    """
    data = await client.get_json(
        "/v2/bank/reconciliation/match",
        params={
            "bankReconciliationId": str(reconciliation_id),
            "approved": "true",
        },
    )
    ids: set[int] = set()
    for match in data.get("values", []):
        for txn in match.get("transactions", []):
            ids.add(txn["id"])
    return ids


async def get_transaction_detail(
    client: TripletexClient,
    transaction_id: int,
) -> dict:
    """Get detailed info for a bank statement transaction.

    GET /v2/bank/statement/transaction/{id}/details
    """
    data = await client.get_json(
        f"/v2/bank/statement/transaction/{transaction_id}/details"
    )
    return data.get("value", data)


def detail_text(detail: object) -> str | None:
    """Pull the narrative line out of a transaction-details payload.

    **The key is localised.** The same endpoint answers
    `{"Detaljer": "DIGITALOCEAN.COM, …"}` on a Norwegian-language company and
    `{"Details": "GOOGLE*WORKSPACE …"}` on an English one — the JSON key follows
    the account language, not just the values. Matching on a fixed name silently
    dropped every line from the English company: card rows on Bonita Services
    carry a merchant 12 times out of 12, and we were reading none of them.

    The payload is a single-entry object, so take the first non-empty string
    rather than guessing which language is in force. An empty `{}` means the
    bank supplied no narrative for that transaction, which is common for
    settlement lines (PayPal, Vipps).
    """
    if not isinstance(detail, dict):
        return None
    for value in detail.values():
        if isinstance(value, str) and value.strip():
            return value
    return None


async def get_unreconciled_transactions(
    client: TripletexClient,
    start_from: date,
    start_to: date,
) -> list[tuple[BankAccount, list[BankTransaction]]]:
    """Get all unreconciled transactions across all bank accounts for a date range.

    Returns list of (account, unreconciled_transactions) tuples.
    """
    accounts = await list_bank_accounts(client)
    accounts = [a for a in accounts if a.require_reconciliation]

    results: list[tuple[BankAccount, list[BankTransaction]]] = []

    periods = await get_periods(client, start_from, start_to)
    if not periods:
        return results

    period_id = periods[0].id

    for account in accounts:
        reconciliation = await get_reconciliation(client, period_id, account.id)
        if reconciliation is None:
            continue

        approved_ids = await get_approved_match_transaction_ids(
            client, reconciliation.id
        )

        unreconciled = [
            t for t in reconciliation.transactions if t.id not in approved_ids
        ]

        # Enrich with transaction details
        for txn in unreconciled:
            txn.details = detail_text(await get_transaction_detail(client, txn.id))

        if unreconciled:
            results.append((account, unreconciled))

    return results
