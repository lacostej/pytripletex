"""System-generated report exports.

These pin the two things that make the export usable as audit evidence: that the
file is named for the account it covers, and that a re-run resumes rather than
duplicating. Both exist because the manual process fails at exactly those points
— the 2025 pack holds `… (1).pdf` through `… (8).pdf`, named by browser
collision.

The scoping and paging facts asserted here were measured against a live ledger on
2026-09-10 and are recorded in the module docstring.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.reports import (
    ReportUnavailable,
    export_fixed_asset_register,
    export_income_statement,
    export_ledger_report,
    export_trial_balance,
    ledger_posting_count,
    ledger_report_filename,
)
from tripletex.session import ApiSession, WebSession, WebSessionRequired

BASE_URL = "https://tripletex.no"

H1 = (datetime.date(2026, 1, 1), datetime.date(2026, 6, 30))

ACCOUNTS = [
    {"id": 1, "number": 1920, "name": "Bankinnskudd - Cafe", "type": "ASSETS"},
    {"id": 2, "number": 6300, "name": "Leie lokale", "type": "OPERATING_EXPENSES"},
    {"id": 3, "number": 1250, "name": "Restaurant/ Cafe, Rekvisita", "type": "ASSETS"},
]


def _client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


#: Every route in this module that returns a document rather than JSON.
REPORT_PATHS = (
    "/v2/ledger/internal/general/",
    "/v2/ledger/balanceSheet/",
    "/v2/execute/listAssets/export/",
    "/execute/resultReport2",
)


def _serving(body: bytes = b"%PDF-1.4 stub", seen: list | None = None):
    """Answer account lists as JSON and report routes as a binary document."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url)
        if request.url.path == "/v2/ledger/internal/general/postingCount":
            return httpx.Response(200, json={"value": 824})
        if any(request.url.path.startswith(p) for p in REPORT_PATHS):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={"values": ACCOUNTS, "fullResultSize": 3})

    return handler


class TestFilename:
    def test_carries_account_and_period(self):
        name = ledger_report_filename(1920, "Bankinnskudd - Cafe", *H1, "pdf")

        assert name == "1920_Bankinnskudd-Cafe_hovedbok_2026-01-01_2026-06-30.pdf"

    def test_slashes_and_commas_do_not_become_path_segments(self):
        """`Restaurant/ Cafe, Rekvisita` is a real account name. A slash here
        would write outside the directory the caller chose."""
        name = ledger_report_filename(1250, "Restaurant/ Cafe, Rekvisita", *H1, "pdf")

        assert "/" not in name
        assert name.startswith("1250_Restaurant-Cafe-Rekvisita_")

    def test_xls_route_is_named_xlsx(self):
        """The route is `xls`; the bytes are a real xlsx (`PK\\x03\\x04`)."""
        assert ledger_report_filename(1920, "Bank", *H1, "xls").endswith(".xlsx")

    def test_unscoped_report_says_so(self):
        assert ledger_report_filename(None, None, *H1, "pdf").startswith("all-accounts_")

    def test_distinct_accounts_never_collide(self):
        """The whole point: no `(1)`/`(2)` suffix, ever."""
        names = {ledger_report_filename(a["number"], a["name"], *H1, "pdf") for a in ACCOUNTS}

        assert len(names) == 3


class TestExportLedgerReport:
    async def test_one_file_per_account_named_for_it(self, tmp_path: Path):
        written = await export_ledger_report(
            _client(_serving()), *H1, tmp_path, accounts=[1920, 6300]
        )

        assert [p.name for p in written] == [
            "1920_Bankinnskudd-Cafe_hovedbok_2026-01-01_2026-06-30.pdf",
            "6300_Leie-lokale_hovedbok_2026-01-01_2026-06-30.pdf",
        ]
        assert all(p.read_bytes() == b"%PDF-1.4 stub" for p in written)

    async def test_scopes_by_account_range_not_query(self, tmp_path: Path):
        """`query` is the UI's search box and reaches the same filter, but drops
        every term after the first — `*1920,*6300` returns account 1920 alone."""
        seen: list[httpx.URL] = []
        await export_ledger_report(
            _client(_serving(seen=seen)), *H1, tmp_path, accounts=[1920, 6300]
        )
        reports = [u for u in seen if u.path.endswith("/general/pdf")]

        assert [u.params["accountNumberFrom"] for u in reports] == ["1920", "6300"]
        assert [u.params["accountNumberTo"] for u in reports] == ["1920", "6300"]
        assert all("query" not in u.params for u in reports)

    async def test_end_date_is_made_exclusive(self, tmp_path: Path):
        """The endpoint takes `dateToExclusive`. A caller asking through
        2026-06-30 must not lose June's last day."""
        seen: list[httpx.URL] = []
        await export_ledger_report(_client(_serving(seen=seen)), *H1, tmp_path, accounts=[1920])
        (report,) = [u for u in seen if u.path.endswith("/general/pdf")]

        assert report.params["dateFrom"] == "2026-01-01"
        assert report.params["dateToExclusive"] == "2026-07-01"

    async def test_rerun_writes_nothing_new(self, tmp_path: Path):
        """An interrupted run must resume, not re-fetch: ~48 report calls per
        company per cycle."""
        calls: list[httpx.URL] = []
        client = _client(_serving(seen=calls))

        first = await export_ledger_report(client, *H1, tmp_path, accounts=[1920])
        downloads = len([u for u in calls if u.path.endswith("/general/pdf")])
        second = await export_ledger_report(client, *H1, tmp_path, accounts=[1920])

        assert second == first
        assert len([u for u in calls if u.path.endswith("/general/pdf")]) == downloads

    async def test_overwrite_refetches(self, tmp_path: Path):
        calls: list[httpx.URL] = []
        client = _client(_serving(seen=calls))

        await export_ledger_report(client, *H1, tmp_path, accounts=[1920])
        await export_ledger_report(client, *H1, tmp_path, accounts=[1920], overwrite=True)

        assert len([u for u in calls if u.path.endswith("/general/pdf")]) == 2

    async def test_unknown_account_is_refused_before_any_download(self, tmp_path: Path):
        """Asking for an account that is not in the chart is a caller mistake.
        Silently exporting the whole ledger under its name would be worse."""
        calls: list[httpx.URL] = []

        with pytest.raises(ValueError, match="9998"):
            await export_ledger_report(
                _client(_serving(seen=calls)), *H1, tmp_path, accounts=[1920, 9998]
            )

        assert not [u for u in calls if u.path.endswith("/general/pdf")]
        assert list(tmp_path.iterdir()) == []

    async def test_no_accounts_exports_the_whole_ledger(self, tmp_path: Path):
        seen: list[httpx.URL] = []
        (written,) = await export_ledger_report(_client(_serving(seen=seen)), *H1, tmp_path)
        (report,) = [u for u in seen if u.path.endswith("/general/pdf")]

        assert written.name.startswith("all-accounts_")
        assert "accountNumberFrom" not in report.params

    async def test_xls_hits_the_xls_route(self, tmp_path: Path):
        seen: list[httpx.URL] = []
        (written,) = await export_ledger_report(
            _client(_serving(body=b"PK\x03\x04stub", seen=seen)),
            *H1, tmp_path, accounts=[1920], fmt="xls",
        )
        (report,) = [u for u in seen if "/general/" in u.path]

        assert report.path.endswith("/general/xls")
        assert "pdfOrientation" not in report.params
        assert written.suffix == ".xlsx"

    async def test_bad_format_is_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="csv"):
            await export_ledger_report(_client(_serving()), *H1, tmp_path, fmt="csv")

    async def test_view_switches_are_sent(self, tmp_path: Path):
        """A report that quietly omits the VAT column is a different document
        from the one the auditor got last year, even if the numbers agree."""
        seen: list[httpx.URL] = []
        await export_ledger_report(_client(_serving(seen=seen)), *H1, tmp_path, accounts=[1920])
        (report,) = [u for u in seen if u.path.endswith("/general/pdf")]

        for switch in ("showVatNumber", "showVoucherNumber", "showPostingDate",
                       "viewRunningTotals", "viewAccountTotals"):
            assert report.params[switch] == "true"


def _web_client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = WebSession(cookies=httpx.Cookies(), context_id="1")
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


#: What /execute/resultReport2 serves for `xls=true` over a real session: an
#: Excel-flavoured HTML table, `application/vnd.ms-excel`, named `.xls`. Also
#: HTML — which is why the refusal below cannot be recognised by being HTML.
EXCEL_HTML = (
    b'<?mso-application progid="Excel.Sheet"?>'
    b"<html><head><title>Resultatrapport</title></head>"
    b"<body><table><tr><td>Salgsinntekt</td><td>1234</td></tr></table></body></html>"
)

#: What Tripletex actually serves a token on /execute/resultReport2 — status 200,
#: content-type text/html, and no hint in either that this is a refusal.
REFUSAL = (
    b"<!DOCTYPE html><html><head><title>Feilsituasjon - Tripletex</title></head>"
    b"<body><h1>Feilsituasjon</h1>Du har ikke tilgang til denne funksjonen.</body></html>"
)


class TestTrialBalance:
    async def test_writes_a_named_file(self, tmp_path: Path):
        seen: list[httpx.URL] = []
        got = await export_trial_balance(_client(_serving(seen=seen)), *H1, tmp_path)
        (req,) = [u for u in seen if "balanceSheet" in u.path]

        assert got.name == "saldobalanse_2026-01-01_2026-06-30.pdf"
        assert req.path == "/v2/ledger/balanceSheet/pdf"
        assert req.params["dateToExclusive"] == "2026-07-01"

    async def test_is_not_under_internal(self, tmp_path: Path):
        """The Hovedbok route is `/v2/ledger/internal/general`; this one is not
        under `internal` at all, and guessing from the sibling 404s."""
        seen: list[httpx.URL] = []
        await export_trial_balance(_client(_serving(seen=seen)), *H1, tmp_path)

        assert not any("internal" in u.path for u in seen)

    async def test_includes_accounts_without_movement(self, tmp_path: Path):
        """An account that moved to zero and one that never existed are
        different findings; only the first shows when this is off."""
        seen: list[httpx.URL] = []
        await export_trial_balance(_client(_serving(seen=seen)), *H1, tmp_path)
        (req,) = [u for u in seen if "balanceSheet" in u.path]

        assert req.params["showAccountsWithoutTransactionsInPeriod"] == "true"

    async def test_rerun_skips(self, tmp_path: Path):
        seen: list[httpx.URL] = []
        client = _client(_serving(seen=seen))

        await export_trial_balance(client, *H1, tmp_path)
        await export_trial_balance(client, *H1, tmp_path)

        assert len([u for u in seen if "balanceSheet" in u.path]) == 1


class TestIncomeStatement:
    async def test_api_token_is_refused_before_the_request(self, tmp_path: Path):
        """A token gets 200 and an HTML error page, so failing up front is the
        only way the caller learns what is actually wrong."""
        seen: list[httpx.URL] = []

        with pytest.raises(WebSessionRequired):
            await export_income_statement(_client(_serving(seen=seen)), *H1, tmp_path)

        assert seen == []
        assert list(tmp_path.iterdir()) == []

    async def test_web_session_downloads(self, tmp_path: Path):
        seen: list[httpx.URL] = []
        got = await export_income_statement(_web_client(_serving(seen=seen)), *H1, tmp_path)
        (req,) = [u for u in seen if "resultReport2" in u.path]

        assert got.name == "resultatrapport_2026-01-01_2026-06-30.pdf"
        assert req.params["pdf"] == "true"

    async def test_end_date_is_inclusive_on_this_route(self, tmp_path: Path):
        """Unlike the /v2 report routes, this one takes the end date as-is.
        Converting it here would drop a day off the far end."""
        seen: list[httpx.URL] = []
        await export_income_statement(_web_client(_serving(seen=seen)), *H1, tmp_path)
        (req,) = [u for u in seen if "resultReport2" in u.path]

        assert req.params["period.startDate"] == "2026-01-01"
        assert req.params["period.endOfPeriodDate"] == "2026-06-30"
        assert "dateToExclusive" not in req.params

    async def test_format_is_chosen_by_presence_not_path(self, tmp_path: Path):
        seen: list[httpx.URL] = []
        got = await export_income_statement(
            _web_client(_serving(body=EXCEL_HTML, seen=seen)), *H1, tmp_path, fmt="xls",
        )
        (req,) = [u for u in seen if "resultReport2" in u.path]

        assert req.params["xls"] == "true"
        assert "pdf" not in req.params

    async def test_spreadsheet_is_a_legacy_xls_not_an_xlsx(self, tmp_path: Path):
        """This route serves Excel-flavoured HTML behind `<?mso-application?>`,
        served as `application/vnd.ms-excel` and named `.xls`. It is a real
        report, not a failure — measured at 149,934 bytes and 139 `<tr>` rows —
        so naming it `.xlsx` would claim a zip container that is not there."""
        got = await export_income_statement(
            _web_client(_serving(body=EXCEL_HTML)), *H1, tmp_path, fmt="xls"
        )

        assert got.suffix == ".xls"
        assert got.read_bytes().startswith(b"<?mso")

    async def test_a_zip_is_not_accepted_from_this_route(self, tmp_path: Path):
        """The guard stays specific per route: an `.xlsx` zip here would mean
        Tripletex changed format, which the caller should hear about."""
        with pytest.raises(ReportUnavailable):
            await export_income_statement(
                _web_client(_serving(body=b"PK\x03\x04stub")), *H1, tmp_path, fmt="xls"
            )

    async def test_no_filter_ids_are_sent_explicitly(self, tmp_path: Path):
        """`-1` means "no filter" on this legacy form; omitting the parameter is
        not the same thing."""
        seen: list[httpx.URL] = []
        await export_income_statement(_web_client(_serving(seen=seen)), *H1, tmp_path)
        (req,) = [u for u in seen if "resultReport2" in u.path]

        assert req.params["selectedCustomerId"] == "-1"
        assert req.params["selectedProjectId"] == "-1"


class TestFixedAssetRegister:
    async def test_writes_a_year_named_file(self, tmp_path: Path):
        seen: list[httpx.URL] = []
        got = await export_fixed_asset_register(_client(_serving(seen=seen)), 2025, tmp_path)
        (req,) = [u for u in seen if "listAssets" in u.path]

        assert got.name == "anleggsregister_2025.pdf"
        assert req.params["year"] == "2025"

    async def test_spreadsheet_route_says_xlsx_not_xls(self, tmp_path: Path):
        """Every other report here spells it `xls`. This one does not, and the
        `xls` spelling 404s."""
        seen: list[httpx.URL] = []
        await export_fixed_asset_register(
            _client(_serving(body=b"PK\x03\x04stub", seen=seen)), 2025, tmp_path, fmt="xls"
        )
        (req,) = [u for u in seen if "listAssets" in u.path]

        assert req.path == "/v2/execute/listAssets/export/xlsx"

    async def test_prior_years_are_requested_unchanged(self, tmp_path: Path):
        """The UI's picker offered only the current year, but the API serves
        earlier ones with differing content. Nothing here should clamp."""
        seen: list[httpx.URL] = []
        client = _client(_serving(seen=seen))

        for year in (2022, 2023, 2024, 2025):
            await export_fixed_asset_register(client, year, tmp_path)

        assert [u.params["year"] for u in seen if "listAssets" in u.path] == [
            "2022", "2023", "2024", "2025",
        ]
        assert len(list(tmp_path.iterdir())) == 4


class TestRefusalIsNotADocument:
    """Tripletex answers 200 with an HTML page when access is denied. Writing
    that into the pack under a `.pdf` name is the failure this guards."""

    def _refusing(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=REFUSAL, headers={"content-type": "text/html"})

    async def test_html_error_page_raises_instead_of_being_saved(self, tmp_path: Path):
        with pytest.raises(ReportUnavailable, match="ikke tilgang"):
            await export_income_statement(_web_client(self._refusing), *H1, tmp_path)

        assert list(tmp_path.iterdir()) == []

    async def test_partial_file_is_removed(self, tmp_path: Path):
        """A leftover file would be skipped by the next run as though it had
        succeeded, making the failure permanent."""
        with pytest.raises(ReportUnavailable):
            await export_trial_balance(_client(self._refusing), *H1, tmp_path)

        assert not (tmp_path / "saldobalanse_2026-01-01_2026-06-30.pdf").exists()

    async def test_an_empty_body_is_not_a_report(self, tmp_path: Path):
        """Measured on the live asset register: dropping `columns` returns 200
        with a content-disposition and zero bytes — a download that looks
        entirely successful and contains nothing."""

        def empty(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"",
                headers={"content-disposition": 'attachment; filename="x.xlsx"'},
            )

        with pytest.raises(ReportUnavailable):
            await export_fixed_asset_register(_client(empty), 2026, tmp_path, fmt="xls")

        assert list(tmp_path.iterdir()) == []

    async def test_columns_is_always_sent(self, tmp_path: Path):
        """The parameter that decides between a real workbook and zero bytes."""
        seen: list[httpx.URL] = []
        await export_fixed_asset_register(
            _client(_serving(body=b"PK\x03\x04stub", seen=seen)), 2026, tmp_path, fmt="xls"
        )
        (req,) = [u for u in seen if "listAssets" in u.path]

        assert "balanceOut" in req.params["columns"]

    async def test_a_zip_is_not_accepted_as_a_pdf(self, tmp_path: Path):
        def wrong_type(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"PK\x03\x04zip")

        with pytest.raises(ReportUnavailable):
            await export_trial_balance(_client(wrong_type), *H1, tmp_path, fmt="pdf")

    async def test_ledger_export_is_guarded_too(self, tmp_path: Path):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/v2/ledger/internal/general/"):
                return httpx.Response(200, content=REFUSAL)
            return httpx.Response(200, json={"values": ACCOUNTS, "fullResultSize": 3})

        with pytest.raises(ReportUnavailable):
            await export_ledger_report(_client(handler), *H1, tmp_path, accounts=[1920])


class TestPostingCount:
    async def test_counts_one_account(self):
        seen: list[httpx.URL] = []

        assert await ledger_posting_count(_client(_serving(seen=seen)), *H1, account=1920) == 824
        assert seen[0].params["accountNumberFrom"] == "1920"

    async def test_never_sends_limit(self):
        """`limit` caps the answer rather than guarding it — on a ledger of
        38 537 postings, `limit=5001` returns exactly 5001. The UI sends it."""
        seen: list[httpx.URL] = []

        await ledger_posting_count(_client(_serving(seen=seen)), *H1)

        assert "limit" not in seen[0].params

    async def test_unscoped_omits_the_account_range(self):
        seen: list[httpx.URL] = []

        await ledger_posting_count(_client(_serving(seen=seen)), *H1)

        assert "accountNumberFrom" not in seen[0].params
