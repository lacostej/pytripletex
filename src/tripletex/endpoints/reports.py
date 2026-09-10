"""System-generated accounting reports — the PDFs and spreadsheets the UI produces.

`ledger.list_postings` returns the same *data*, and a caller can render its own
CSV from it. This module exists because for audit evidence that is not the same
thing: a report the accounting system produced carries weight that one we
rendered from the same API does not.

**These are `/v2` paths and they accept an API token**, despite `internal` in the
route and despite being absent from the published specification. Measured
2026-09-10: `GET /v2/ledger/internal/general/pdf` answers 200 with `%PDF-1.4`
under token auth. So the whole pipeline is schedulable — no web session, unlike
SAF-T 1.3.

**Tripletex discards the account from the filename.** Ask for one account and the
download is still named `<Company>_Hovedbok_<today>_(<from> - <to>).pdf`,
identical for every account. That is why a manually assembled pack ends up
holding `… (1).pdf` through `… (8).pdf`, numbered by browser collision, with
nothing but the enclosing folder saying which account each covers.

There is a reason for it rather than mere carelessness: the report scopes by a
**range** of accounts, so in general there is no single account to name a file
after. Measured over 2025 by posting count: `1920..1920` 824, `6300..6300` 89,
`6000..7999` 2 310, the whole ledger 38 537. The library exports one account per
call anyway, which makes the name well-defined again.

`query` is what the UI sends, and its account forms reach exactly this filter —
`*1920`, `*1920-1920`, `*6000-`, `*-1920` each return the same posting count as
the equivalent `accountNumberFrom`/`accountNumberTo` pair, checked term by term.
The explicit pair is used here because it says what it means.

The rest of `query`'s syntax, measured against a 38 537-posting year:

    *1920           one account                             824
    *6000-7999      account range                         2 310
    *6000-          from 6000 up                          2 334
    *-1920          up to 1920                            5 715
    #869            voucher number, any year                 12
    Oppvask         substring of the description             73
    (empty)         no filter                            38 537

`#869` was confirmed against the documented API: voucher 869 holds exactly 12
postings, `#1920` exactly 11. It ignores the voucher's year, so a periodisering
booked in 2023 is still found by number in a 2025 report.

**There is no way to ask for two accounts at once.** No separator works —
`*1920 *6300`, `|` and `OR` all match nothing, while a comma or semicolon
silently returns account 1920 alone. Ranges are the only multi-account form, and
`#` does not accept them even though `*` does. Avoid `%` and `_` in a search
term: they are not literal, and they change the result in a way this measurement
did not explain (every position tried returned the same narrowed count).

Every function here therefore takes a *directory* and derives the filename itself
from what the call already knows — account number, name, period and format —
following `vouchers.backup_all_vouchers`, which does the same and skips files
that already exist so a run resumes.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from tripletex.endpoints.ledger import list_accounts

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

#: Column and formatting switches the UI sends. Kept verbatim rather than
#: trimmed: they are what makes the output match the report an auditor was given
#: last year, and a report that quietly omits the VAT column is a different
#: document even if the numbers agree.
_LEDGER_VIEW = {
    "sortBy": "date_asc",
    "showClosedStatus": "true",
    "showVoucherNumber": "true",
    "showPostingDate": "true",
    "showDescription": "true",
    "showVatNumber": "true",
    "showAmountCurrency": "true",
    "showAmount": "true",
    "viewRunningTotals": "true",
    "viewAccountTotals": "true",
}

#: `xls` is the route; the file it returns is a real `.xlsx`. Measured: the body
#: begins `PK\\x03\\x04` and Content-Disposition names it `.xlsx`.
_SUFFIX = {"pdf": "pdf", "xls": "xlsx"}


def _slug(text: str, limit: int = 40) -> str:
    """A filename-safe fragment of an account name.

    Norwegian names carry slashes and commas — `Restaurant/ Cafe Rekvisita` —
    either of which makes a path that is not what the caller asked for.
    """
    cleaned = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    return re.sub(r"[\s_-]+", "-", cleaned)[:limit].strip("-")


def ledger_report_filename(
    account_number: int | None,
    account_name: str | None,
    date_from: date,
    date_to: date,
    fmt: str,
) -> str:
    """The name Tripletex should have used.

    Unique per account and period, readable without its folder, and stable
    across runs so a second run skips rather than duplicating.
    """
    scope = f"{account_number}_{_slug(account_name or '')}".rstrip("_") if account_number else "all-accounts"
    return f"{scope}_hovedbok_{date_from}_{date_to}.{_SUFFIX[fmt]}"


async def ledger_posting_count(
    client: TripletexClient,
    date_from: date,
    date_to: date,
    account: int | None = None,
) -> int:
    """How many postings a ledger report would cover. Cheap — no document is built.

    GET /v2/ledger/internal/general/postingCount, token auth.

    Lets a caller tell an account with nothing to report from an export that
    failed, which is not something the downloaded file answers: Tripletex returns
    a valid, well-formed, empty report either way. Whether an empty account is
    worth a file is the caller's decision, not this library's.

    **`limit` is deliberately not sent.** The UI passes `limit=5001` and the
    parameter *caps* the answer rather than guarding it — measured on a ledger of
    38 537 postings, `limit=5001` returns exactly `5001`. Anything copied from a
    captured UI request will quietly under-report a busy period.
    """
    params = {
        "dateFrom": date_from.isoformat(),
        "dateToExclusive": (date_to + timedelta(days=1)).isoformat(),
    }
    if account is not None:
        params["accountNumberFrom"] = str(account)
        params["accountNumberTo"] = str(account)

    body = await client.get_json("/v2/ledger/internal/general/postingCount", params)
    return int(body["value"])


async def export_ledger_report(
    client: TripletexClient,
    date_from: date,
    date_to: date,
    dest_dir: Path | str,
    accounts: list[int] | None = None,
    fmt: str = "pdf",
    overwrite: bool = False,
) -> list[Path]:
    """Hovedbok — the general ledger — one file per account. Returns paths written.

    GET /v2/ledger/internal/general/{pdf,xls}, token auth.

    `accounts` are chart-of-accounts *numbers*, not ids. Each becomes its own
    request, scoped by `accountNumberFrom`/`accountNumberTo` set to that one
    number; scoping is real, not cosmetic — measured over one half-year, account
    1920 yields 381 rows and 6300 yields 50, against 19 496 for the whole ledger.

    Not `query`, the UI's search box, though it reaches the same filter — see the
    module docstring. Exporting several accounts into one file is possible
    (`accountNumberFrom=6000&accountNumberTo=7999`) and deliberately not offered:
    a file covering a range has no account to be named after, which is the whole
    problem this module exists to fix.

    Passing `None` exports the entire ledger as a single file, which is what the
    UI does by default and is rarely what an audit pack wants.

    **`date_to` is inclusive here.** The endpoint takes `dateToExclusive`, and
    this converts, so a caller asking for `2026-06-30` gets June included rather
    than silently losing its last day.

    Existing files are skipped unless `overwrite`, so an interrupted run resumes
    without re-fetching. That matters at ~48 report calls per company.
    """
    if fmt not in _SUFFIX:
        raise ValueError(f"fmt must be 'pdf' or 'xls', not {fmt!r}")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    names: dict[int, str] = {}
    if accounts:
        names = {
            a.number: (a.name or "")
            for a in await list_accounts(client)
            if a.number in set(accounts)
        }
        missing = [n for n in accounts if n not in names]
        if missing:
            raise ValueError(f"No such account(s) in the chart of accounts: {missing}")

    params = {
        **_LEDGER_VIEW,
        "dateFrom": date_from.isoformat(),
        # The endpoint's end date is exclusive; the caller's is not.
        "dateToExclusive": (date_to + timedelta(days=1)).isoformat(),
    }
    if fmt == "pdf":
        params["pdfOrientation"] = "PORTRAIT"

    written: list[Path] = []
    for number in accounts or [None]:
        target = dest_dir / ledger_report_filename(
            number, names.get(number) if number else None, date_from, date_to, fmt
        )
        if target.exists() and not overwrite:
            logger.info("Skipping %s — already present", target.name)
            written.append(target)
            continue

        scoped = dict(params)
        if number is not None:
            # A degenerate range — one account at both ends.
            scoped["accountNumberFrom"] = str(number)
            scoped["accountNumberTo"] = str(number)

        await client.download(f"/v2/ledger/internal/general/{fmt}", scoped, target)
        written.append(target)

    return written
