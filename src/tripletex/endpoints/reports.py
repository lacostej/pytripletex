"""System-generated accounting reports — the PDFs and spreadsheets the UI produces.

`ledger.list_postings` returns the same *data*, and a caller can render its own
CSV from it. This module exists because for audit evidence that is not the same
thing: a report the accounting system produced carries weight that one we
rendered from the same API does not.

None of these routes appear in the published specification, and they share
neither a prefix nor an auth mode. Measured 2026-09-10, both companies:

    Hovedbok          /v2/ledger/internal/general/{pdf,xls}      token
    Saldobalanse      /v2/ledger/balanceSheet/{pdf,xls}          token
    Anleggsregister   /v2/execute/listAssets/export/{pdf,xlsx}   token
    Resultatrapport   /execute/resultReport2                     WEB SESSION

Note `balanceSheet` is not under `internal`, `listAssets` sits under `/v2/execute`
and spells the spreadsheet `xlsx` rather than `xls`, and `resultReport2` is a
legacy form route that selects its format with `pdf=true`/`xls=true`. Guessing
any of them from the shape of the others does not work.

The spreadsheets are not one format either. The `/v2` routes return a real
`.xlsx` zip; Resultatrapport returns Excel-flavoured HTML behind an
`<?mso-application?>` instruction, served as `application/vnd.ms-excel` and named
`.xls` — measured at 149 934 bytes over 139 rows. That is a genuine legacy `.xls`
and not a defect, so it is named `.xls` here rather than claiming a zip container
that is not present.

**Three of the four are schedulable**, which SAF-T 1.3 is not. The income
statement is the exception, and it fails dangerously rather than loudly: under a
token it answers **200** with an HTML page reading `Du har ikke tilgang til denne
funksjonen`. Every download here therefore checks the leading bytes and raises
`ReportUnavailable` rather than leaving an error page in the pack under a `.pdf`
name — where the next run would skip it as already done, making the failure
permanent and silent.

Note what that check cannot be: **a valid Resultatrapport spreadsheet and a
refusal are both HTML.** "Did we get HTML?" answers neither question. The
expected prefix is therefore per route and per format, not per format alone.

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
from tripletex.session import require_web_session

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

#: What each format must actually start with. Checked on every download because
#: one of these routes answers 200 with an HTML error page — see
#: `_download_checked`. The `/v2` routes return a real `.xlsx` zip; the legacy
#: Resultatrapport returns Excel-flavoured HTML behind an `<?mso-application?>`
#: instruction, which is a genuine `.xls` and not a defect.
_PDF = b"%PDF"
_XLSX = b"PK\x03\x04"
_XLS_HTML = b"<?mso"

#: Saldobalanse. `showAccountsWithoutTransactionsInPeriod` is deliberately on: an
#: account that moved to zero and one that never existed are different findings,
#: and only the first appears when it is off.
_TRIAL_BALANCE_VIEW = {
    "showAccountsWithoutTransactionsInPeriod": "true",
    "pageFormat": "A4",
}

#: Resultatrapport. The `selected*Id=-1` values mean "no filter" — the report is
#: a legacy `/execute/` form, and omitting them is not the same as passing -1.
_INCOME_STATEMENT_VIEW = {
    "viewMode": "0",
    "isExpandedFilter": "false",
    "period.periodType": "1",
    "selectedCustomerId": "-1",
    "selectedVendorId": "-1",
    "selectedDepartmentId": "-1",
    "selectedEmployeeId": "-1",
    "selectedProjectId": "-1",
    "selectedProjectCategoryId": "-1",
    "selectedProductId": "-1",
    "budgetType": "0",
    "viewAccounts": "true",
    "viewLastYear": "true",
    "viewAccountingPeriods": "true",
    "viewSoFar": "false",
    "viewUnusedReportGroups": "false",
    "showDecimalVerdi": "false",
}

#: Anleggsregister. `columns` is a comma-space separated list and its order is
#: the column order in the output.
#:
#: **`columns` is load-bearing — do not drop it to simplify this.** Omitted, the
#: route answers `200` with a `content-disposition` and a **zero-byte body**:
#: a download that looks entirely successful and contains nothing. `groupBy` and
#: `sorting` make no difference either way; `columns` alone decides it. The
#: magic-byte check in `_download_checked` is what stops such a file reaching the
#: pack, and `TestRefusalIsNotADocument` pins that.
_ASSET_REGISTER_VIEW = {
    "sorting": "name,ascending",
    "groupBy": "ACCOUNT",
    "query": "",
    "columns": (
        "depreciationMethod, status, dateOfAcquisition, lifetime, "
        "balanceIn, balanceChange, balanceOut"
    ),
}


class ReportUnavailable(RuntimeError):
    """Tripletex answered, but with something other than the report.

    Distinct from a transport error on purpose: the completeness check needs to
    tell "this report does not exist for this period" from "the download broke".
    """


def _validate_format(fmt: str) -> None:
    if fmt not in _SUFFIX:
        raise ValueError(f"fmt must be 'pdf' or 'xls', not {fmt!r}")


async def _download_checked(
    client: TripletexClient,
    path: str,
    params: dict[str, str],
    target: Path,
    expect: bytes,
) -> Path:
    """Download, then refuse anything that is not the document it claims to be.

    `/execute/resultReport2` answers **200 with an HTML page** when the caller
    lacks access — `Feilsituasjon … Du har ikke tilgang til denne funksjonen` —
    so the status code cannot distinguish a report from a refusal, and an
    unchecked download leaves that page sitting in the pack under a `.pdf` name,
    looking like evidence.

    **Do not reach for `content-type` instead.** The three `/v2` routes get it
    wrong in the direction that costs most: a response whose body begins `%PDF`
    is served as `application/json;charset=UTF-8`, so trusting the header would
    reject every real report and keep nothing. The legacy Resultatrapport is by
    contrast honest — `application/pdf`, `application/vnd.ms-excel`, `text/html`
    for the refusal — which is worse than uniform dishonesty, because a header
    that is right three times in four invites exactly the guard that fails on the
    fourth. `content-disposition` is accurate everywhere, but it describes what
    the server meant to send; the bytes describe what arrived, and only the
    second is evidence.
    """
    await client.download(path, params, target)

    head = target.read_bytes()[:1024]
    if head.startswith(expect):
        return target

    target.unlink(missing_ok=True)
    complaint = " ".join(re.sub(r"<[^>]+>", " ", head.decode("utf-8", "replace")).split())
    raise ReportUnavailable(
        f"{path} returned {len(head)}+ bytes not starting {expect!r}: {complaint[:200]}"
    )


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

    **A PDF is capped at 15 000 postings and a whole year usually exceeds it.**
    Above the cap the route answers `422` — `PDF-eksport er begrenset til 15000
    posteringer per fil` — and `client.download` raises, so this fails loudly
    rather than truncating. Measured: an unscoped 2025 (38 537 postings) is
    refused as PDF, while the same request as `fmt="xls"` returns all of it in
    2.1 MB. The cap is per *file*, so per-account exports stay well under it —
    the busiest account measured was 3 445. Use `xls`, a shorter period, or
    accounts, in that order of preference.

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

        await _download_checked(
            client,
            f"/v2/ledger/internal/general/{fmt}",
            scoped,
            target,
            _PDF if fmt == "pdf" else _XLSX,
        )
        written.append(target)

    return written


async def export_trial_balance(
    client: TripletexClient,
    date_from: date,
    date_to: date,
    dest_dir: Path | str,
    fmt: str = "pdf",
    overwrite: bool = False,
) -> Path:
    """Saldobalanse — every account's opening balance, movement and close.

    GET /v2/ledger/balanceSheet/{pdf,xls}, token auth.

    Note this one is **not** under `internal`, unlike the Hovedbok route.
    """
    _validate_format(fmt)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    target = dest_dir / f"saldobalanse_{date_from}_{date_to}.{_SUFFIX[fmt]}"
    if target.exists() and not overwrite:
        logger.info("Skipping %s — already present", target.name)
        return target

    params = {
        **_TRIAL_BALANCE_VIEW,
        "dateFrom": date_from.isoformat(),
        "dateToExclusive": (date_to + timedelta(days=1)).isoformat(),
    }
    if fmt == "pdf":
        params["pdfOrientation"] = "PORTRAIT"

    return await _download_checked(
        client,
        f"/v2/ledger/balanceSheet/{fmt}",
        params,
        target,
        _PDF if fmt == "pdf" else _XLSX,
    )


async def export_income_statement(
    client: TripletexClient,
    date_from: date,
    date_to: date,
    dest_dir: Path | str,
    fmt: str = "pdf",
    overwrite: bool = False,
) -> Path:
    """Resultatrapport — the income statement. **Web session only.**

    GET /execute/resultReport2, a legacy form route rather than a `/v2` path.

    This is the one report in this module a token cannot reach, and it fails in
    the worst possible way: an API token gets **200** with an HTML page saying
    `Du har ikke tilgang til denne funksjonen`, not a 401. The session is
    therefore checked before the request, and the bytes after it.

    Format is selected by presence — `pdf=true` or `xls=true` — not by the path.

    **There is no way to get a real `.xlsx` here, so do not go looking.**
    `xlsx=true`, `excel=true`, `ods=true`, `format=xlsx` and `exportFormat=XLSX`
    are all silently ignored: each returns the same ~26 KB on-screen HTML page
    that sending no format parameter at all returns, with no
    `content-disposition`. An `Accept:` header for the OpenXML type changes
    nothing either. The route's `.xls` is legacy Excel HTML, which Excel opens
    natively — it is not a damaged `.xlsx`.

    A third format exists and is not exposed here: `csv=true` yields the same
    report as 115 rows carrying every month, the year total and the prior-year
    comparison. It is worth knowing about, and worth two warnings if it is ever
    added — it is **tab**-separated despite the name, and **ISO-8859-1 despite
    declaring `charset=UTF-8`**, so decoding it as the header instructs raises
    `UnicodeDecodeError` on the first Norwegian vowel.
    """
    _validate_format(fmt)
    require_web_session(client.session, "The income statement report")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Not `_SUFFIX`: this route's spreadsheet is a legacy `.xls`, not an `.xlsx`.
    suffix = "pdf" if fmt == "pdf" else "xls"
    target = dest_dir / f"resultatrapport_{date_from}_{date_to}.{suffix}"
    if target.exists() and not overwrite:
        logger.info("Skipping %s — already present", target.name)
        return target

    params = {
        **_INCOME_STATEMENT_VIEW,
        "period.startDate": date_from.isoformat(),
        # This route's end date is inclusive, unlike the /v2 report routes.
        "period.endOfPeriodDate": date_to.isoformat(),
    }
    if fmt == "pdf":
        params["pdf"] = "true"
        params["pdfSize"] = "A4 landscape"
        params["menuHeader"] = "Resultatrapport"
    else:
        params["xls"] = "true"

    return await _download_checked(
        client,
        "/execute/resultReport2",
        params,
        target,
        _PDF if fmt == "pdf" else _XLS_HTML,
    )


async def export_fixed_asset_register(
    client: TripletexClient,
    year: int,
    dest_dir: Path | str,
    fmt: str = "pdf",
    overwrite: bool = False,
) -> Path:
    """Anleggsregister — the fixed asset register, as at a financial year.

    GET /v2/execute/listAssets/export/{pdf,xlsx}, token auth.

    Takes a **year**, not a date, because the endpoint does: passing an `as_of`
    date would mean silently discarding its month and day.

    **The API accepts years the UI refuses.** The UI's picker offered only the
    current year, but 2022 through 2027 all return a document, and the cell
    content differs for each — compared with the workbook metadata excluded, so
    this is not just an embedded timestamp moving. Prior years are reachable
    here even though a person cannot select them.
    """
    _validate_format(fmt)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    target = dest_dir / f"anleggsregister_{year}.{_SUFFIX[fmt]}"
    if target.exists() and not overwrite:
        logger.info("Skipping %s — already present", target.name)
        return target

    params = {**_ASSET_REGISTER_VIEW, "year": str(year)}
    if fmt == "pdf":
        params["pdfOrientation"] = "PORTRAIT"

    # This route spells the spreadsheet `xlsx`, where the ledger routes say `xls`.
    segment = "pdf" if fmt == "pdf" else "xlsx"
    return await _download_checked(
        client,
        f"/v2/execute/listAssets/export/{segment}",
        params,
        target,
        _PDF if fmt == "pdf" else _XLSX,
    )
