"""Chart of accounts, VAT types, and postings.

The rest of the library reads vouchers as headers — id, number, date, whether a
document is attached. This module reads them as *bookkeeping*: which account each
line hit, which VAT treatment it was given, and for how much. That is what any
check on classification, VAT handling or cost leakage needs.

**Postings come back inside the voucher, in one request.** `fields` expands them
fully, so a month of a company is a single call — 326 vouchers and 3031 postings
in one measured July, not 327. Prefer `list_vouchers_with_postings` over walking
`/v2/ledger/posting` and re-joining: the posting endpoint gives you a line
without the receipt, and the receipt is half of every interesting question.

All of it works with API-token auth, so these checks can be scheduled — unlike
payments, which remain web-session only.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from tripletex.endpoints._paging import paginate
from tripletex.models import Account, LedgerVoucher, Posting, VatType

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

_ACCOUNT_FIELDS = "id,number,name,type,vatType(id,name,percentage),isBankAccount,isInactive"

_POSTING_FIELDS = (
    "id,date,description,amount,amountCurrency,currency(code),"
    "account(id,number,name,type),vatType(id,name,percentage),"
    "supplier(id,name),customer(id,name),employee(id),"
    "department(id,name),project(id,name),row"
)

#: Voucher header plus everything needed to judge the bookkeeping. `changes`
#: is excluded — it roughly doubles the payload and only the timing checks want it.
_VOUCHER_POSTING_FIELDS = (
    "id,number,tempNumber,year,date,description,voucherType(id,name),"
    "attachment(id,fileName,size,mimeType)," + f"postings({_POSTING_FIELDS})"
)


async def list_accounts(
    client: TripletexClient,
    include_inactive: bool = True,
) -> list[Account]:
    """The chart of accounts.

    GET /v2/ledger/account. 603 rows on Bonita Handel, so it pages once and is
    worth caching for the length of a run — every posting check joins against it.
    """
    params = {"fields": _ACCOUNT_FIELDS}
    if not include_inactive:
        params["isInactive"] = "false"
    values = await paginate(client, "/v2/ledger/account", params=params)
    return [Account.model_validate(a) for a in values]


async def list_vat_types(client: TripletexClient) -> list[VatType]:
    """Every VAT treatment the company can apply.

    GET /v2/ledger/vatType. 56 rows, of which one measured month used 6.
    """
    values = await paginate(
        client, "/v2/ledger/vatType", params={"fields": "id,name,percentage"}
    )
    return [VatType.model_validate(v) for v in values]


async def list_voucher_types(client: TripletexClient) -> dict[int, str]:
    """Voucher types by id, for labelling.

    GET /v2/ledger/voucherType — 17 rows. Returned as a dict because that is how
    every caller uses it. Note that a voucher's `voucherType` may be `None`,
    which this cannot tell you about; see `LedgerVoucher.voucher_type_name`.
    """
    values = await paginate(
        client, "/v2/ledger/voucherType", params={"fields": "id,name"}
    )
    return {v["id"]: v.get("name", "") for v in values if v.get("id")}


async def list_vouchers_with_postings(
    client: TripletexClient,
    date_from: date,
    date_to: date,
    include_changes: bool = False,
    limit: int | None = None,
) -> list[LedgerVoucher]:
    """Vouchers over a date range, with receipt and postings expanded.

    GET /v2/ledger/voucher with an expanded `fields`. This is the workhorse for
    every accounting check — one request per page gives header, attachment and
    full posting detail together.

    Set `include_changes` for the CREATE/UPDATE log, which is how documents are
    aged and queue dwell time is derived. It costs payload, so it is off by
    default. Be aware the log records no distinct *booking* event: the last
    UPDATE is a proxy for when the voucher was booked, not a measurement of it.
    """
    fields = _VOUCHER_POSTING_FIELDS
    if include_changes:
        fields += ",changes"

    params = {
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "fields": fields,
    }
    values = await paginate(client, "/v2/ledger/voucher", params=params, limit=limit)
    return [LedgerVoucher.model_validate(v) for v in values]


async def list_close_groups(
    client: TripletexClient,
    date_from: date,
    date_to: date,
    limit: int | None = None,
) -> list[dict]:
    """Open-item matches — lukkegrupper — with their postings expanded.

    GET /v2/ledger/closeGroup. A close group is Tripletex's record that an open
    item was settled: it holds the posting that opened the item and the
    posting(s) that closed it. That makes it the only place the API tells you
    *when* something was paid — an invoice reports `amountOutstanding: 0` once
    settled, but never the date it happened.

    So this is what any payment-latency measure has to be built on. Measured on
    Bonita Handel Jan–Aug 2026: 1228 groups, of which 841 touch supplier debt
    (2400) and 33 customer receivables (1500). The rest are accruals and other
    matched balance-sheet items, so callers must filter by account rather than
    assuming every group is a payment.

    Returned as raw dicts: a close group is a join record with no useful identity
    of its own, and every caller wants the postings rather than the wrapper.
    """
    params = {
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "fields": (
            "id,date,postings(id,date,amount,account(number,name),"
            "customer(id,name),supplier(id,name),voucher(id,number))"
        ),
    }
    return await paginate(client, "/v2/ledger/closeGroup", params=params, limit=limit)


async def list_postings(
    client: TripletexClient,
    date_from: date,
    date_to: date,
    account_numbers: list[int] | None = None,
    limit: int | None = None,
) -> list[Posting]:
    """Postings over a date range, optionally narrowed to specific accounts.

    GET /v2/ledger/posting. Use this when the account is the subject — sweeping
    fee and interest accounts, say — and the receipt does not matter. When it
    does, use `list_vouchers_with_postings` instead: this endpoint does not
    carry the voucher's attachment.

    `account_numbers` filters client-side. The endpoint takes an `accountId`
    parameter, but that wants internal ids rather than account numbers, and
    resolving those costs the account fetch this avoids.
    """
    params = {
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "fields": _POSTING_FIELDS + ",voucher(id,number)",
    }
    values = await paginate(client, "/v2/ledger/posting", params=params, limit=limit)
    postings = [Posting.model_validate(p) for p in values]

    if account_numbers is not None:
        wanted = set(account_numbers)
        postings = [
            p for p in postings if p.account and p.account.number in wanted
        ]
    return postings
