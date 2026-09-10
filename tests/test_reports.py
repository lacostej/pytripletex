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
    export_ledger_report,
    ledger_posting_count,
    ledger_report_filename,
)
from tripletex.session import ApiSession

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


def _serving(body: bytes = b"%PDF-1.4 stub", seen: list | None = None):
    """Answer account lists as JSON and report routes as a binary document."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url)
        if request.url.path == "/v2/ledger/internal/general/postingCount":
            return httpx.Response(200, json={"value": 824})
        if request.url.path.startswith("/v2/ledger/internal/general/"):
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
