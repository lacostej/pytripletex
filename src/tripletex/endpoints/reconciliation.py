"""Bank reconciliation endpoints: accounts, periods, unreconciled transactions."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import TYPE_CHECKING

from tripletex.endpoints._paging import paginate
from tripletex.models import AccountingPeriod, BankAccount, BankTransaction, Reconciliation

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


class Enrichment(str, Enum):
    """How hard to work at filling in `BankTransaction.details`.

    Enrichment costs **one request per unmatched transaction**, which dominates
    the cost of listing a month: 19 transactions across four companies meant 19
    extra calls on top of roughly three per account. A caller that only needs
    counts and dates should not pay it.

    Two strategies for now. The value of `details` varies by bank — a merchant
    name on some, a transaction type or nothing on others — so a selective
    strategy is a real possibility later (`CARDS`, say, enriching only
    card-shaped descriptions). This is an enum rather than a boolean so adding
    one does not change the signature again.
    """

    NONE = "none"
    """Leave `details` unset. The default: cheap, and enough for counting."""

    ALL = "all"
    """Fetch details for every unmatched transaction."""


def normalize_enrichment(enrich: Enrichment | str | None) -> Enrichment:
    """Accept `None`, a name, or a member; reject anything else up front.

    `None` means `NONE` — it reads naturally at a call site and keeps the common
    case free of an import.
    """
    if enrich is None:
        return Enrichment.NONE
    if isinstance(enrich, Enrichment):
        return enrich
    try:
        return Enrichment(str(enrich).strip().lower())
    except ValueError:
        valid = ", ".join(e.value for e in Enrichment)
        raise ValueError(
            f"Unknown enrichment strategy {enrich!r}. Valid values: {valid}"
        ) from None


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


async def list_reconciliations(
    client: TripletexClient,
    limit: int | None = None,
) -> list[dict]:
    """Every bank reconciliation, with when it was closed and by whom.

    GET /v2/bank/reconciliation. `accountingPeriod.start` is the month being
    reconciled and `closedDate` is when the work was actually finished, so the
    gap between them is the reconciliation lag — how long after a month ends its
    bank accounts get squared away. Measured on Bonita Handel, the January 2022
    reconciliation closed on 2022-04-09.

    That gap is the only reconciliation timing the API exposes. Individual
    transactions carry a `postedDate` but no matched-on date; the closing of the
    reconciliation is the event with a timestamp.

    Returned as raw dicts because callers want different slices of a wide object
    and `Reconciliation` deliberately models only the balance-checking fields.
    """
    params = {
        "fields": (
            "id,isClosed,closedDate,type,closedByEmployee(id,firstName,lastName),"
            "account(id,number,name),accountingPeriod(id,start,end),transactions(id)"
        )
    }
    return await paginate(client, "/v2/bank/reconciliation", params=params, limit=limit)


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
    enrich: Enrichment | str | None = None,
) -> list[tuple[BankAccount, list[BankTransaction]]]:
    """Get all unreconciled transactions across all bank accounts for a date range.

    Returns list of (account, unreconciled_transactions) tuples.

    `enrich` controls whether `BankTransaction.details` is filled in, which costs
    one extra request per unmatched transaction. It defaults to
    `Enrichment.NONE`: counts, dates and amounts arrive either way, and that is
    what a monitor needs. Pass `Enrichment.ALL` for the narrative line — worth it
    on a card row, where `description` is only a masked card number.

    **This default changed.** Enrichment used to be unconditional, so a caller
    relying on `details` must now ask for it.
    """
    strategy = normalize_enrichment(enrich)

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

        if strategy is Enrichment.ALL:
            for txn in unreconciled:
                txn.details = detail_text(await get_transaction_detail(client, txn.id))

        if unreconciled:
            results.append((account, unreconciled))

    return results
