"""SAF-T export.

Verified against the live API 2026-09-09: one company's 2025 export came back as
a 449 817-byte zip holding an 11.4 MB XML at `AuditFileVersion 1.20`. The
organisation number in the sample filename is a placeholder.

The version check is the point of `audit_file_version`. A 2023 web-produced
export in this estate reads `1.10`, this endpoint emits `1.20`, and Tripletex's
web UI now warns about codes invalid for `1.3` — so "a SAF-T file" is not one
thing, and a caller filing one needs to know which it has.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.saft import (
    audit_file_version,
    export_saft,
    export_saft_web,
)
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

XML = """<?xml version="1.0" encoding="UTF-8"?>
<AuditFile xmlns="urn:StandardAuditFile-Taxation-Financial:NO">
  <Header>
    <AuditFileVersion>1.20</AuditFileVersion>
    <AuditFileCountry>NO</AuditFileCountry>
  </Header>
  <MasterFiles><GeneralLedgerAccounts>
    <Account><AccountID>8960</AccountID><StandardAccountID>89</StandardAccountID></Account>
    <Account><AccountID>9999</AccountID><StandardAccountID>NA</StandardAccountID></Account>
  </GeneralLedgerAccounts></MasterFiles>
</AuditFile>
"""


def _zip_bytes(*members: tuple[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members:
            zf.writestr(name, text)
    return buf.getvalue()


ARCHIVE = _zip_bytes(("SAF-T Financial_000000000_20260909170503_1_1.xml", XML))


def _client(payload: bytes = ARCHIVE) -> TripletexClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload,
            headers={"content-type": "application/octet-stream"},
        )

    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


class TestExport:
    async def test_writes_the_archive(self, tmp_path):
        out = await export_saft(_client(), 2025, tmp_path / "saft.zip")

        assert out.read_bytes() == ARCHIVE
        assert zipfile.is_zipfile(out)

    async def test_a_directory_destination_is_named_by_year_and_version(self, tmp_path):
        """1.2 and 1.3 are different documents, so two exports of one year must
        not overwrite each other."""
        out = await export_saft(_client(), 2025, tmp_path)

        assert out.name == "saft-2025-v1.3.zip"
        assert out.parent == tmp_path

        older = await export_saft(_client(), 2025, tmp_path, version="1.2")

        assert older.name == "saft-2025-v1.2.zip"
        assert older != out

    async def test_sends_the_year(self):
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, content=ARCHIVE)

        client = TripletexClient(TripletexConfig(base_url=BASE_URL))
        client._session = ApiSession(session_token="tok", company_id=0)
        client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BASE_URL
        )
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            await export_saft(client, 2025, pathlib.Path(d) / "x.zip")

        assert seen[0].params["year"] == "2025"
        assert seen[0].path == "/v2/saft/exportSAFT"

    async def test_extract_unpacks_the_inner_xml(self, tmp_path):
        out = await export_saft(_client(), 2025, tmp_path / "saft.zip", extract=True)

        assert out.suffix == ".xml"
        assert out.name.startswith("SAF-T Financial_")
        assert b"AuditFileVersion" in out.read_bytes()

    async def test_extract_refuses_an_archive_with_several_xml(self, tmp_path):
        """Picking the first would quietly produce the wrong filing."""
        payload = _zip_bytes(("a.xml", XML), ("b.xml", XML))

        with pytest.raises(RuntimeError, match="found 2"):
            await export_saft(_client(payload), 2025, tmp_path / "s.zip", extract=True)

    async def test_extract_refuses_an_archive_with_no_xml(self, tmp_path):
        payload = _zip_bytes(("readme.txt", "nothing here"))

        with pytest.raises(RuntimeError, match="found 0"):
            await export_saft(_client(payload), 2025, tmp_path / "s.zip", extract=True)

    async def test_a_nested_member_path_cannot_escape(self, tmp_path):
        """A member named `../evil.xml` must land beside the archive, not above."""
        payload = _zip_bytes(("../evil.xml", XML))

        out = await export_saft(_client(payload), 2025, tmp_path / "s.zip", extract=True)

        assert out.parent == tmp_path
        assert out.name == "evil.xml"


class TestAuditFileVersion:
    """"A SAF-T file" is not one thing — 1.10, 1.20 and 1.3 are all in play."""

    def test_reads_it_from_a_zip_without_unpacking(self, tmp_path):
        archive = tmp_path / "s.zip"
        archive.write_bytes(ARCHIVE)

        assert audit_file_version(archive) == "1.20"

    def test_reads_it_from_a_plain_xml(self, tmp_path):
        path = tmp_path / "s.xml"
        path.write_text(XML)

        assert audit_file_version(path) == "1.20"

    def test_namespaced_tag_is_matched(self, tmp_path):
        """The document is namespaced, so a bare tag lookup finds nothing."""
        path = tmp_path / "s.xml"
        path.write_text(XML)

        assert "xmlns=" in XML
        assert audit_file_version(path) is not None

    def test_absent_version_is_none_not_an_error(self, tmp_path):
        path = tmp_path / "s.xml"
        path.write_text('<AuditFile xmlns="urn:x"><Header/></AuditFile>')

        assert audit_file_version(path) is None

    def test_zip_without_xml_is_none(self, tmp_path):
        archive = tmp_path / "s.zip"
        archive.write_bytes(_zip_bytes(("readme.txt", "x")))

        assert audit_file_version(archive) is None


class TestWebExport:
    """The web route, read off `/saftExport.js`.

    It reaches what the token API cannot: an arbitrary date range, and version
    1.3. The API emits 1.2 only and takes no version argument.
    """

    def _web(self, handler) -> TripletexClient:
        from tripletex.session import WebSession

        client = TripletexClient(TripletexConfig(base_url=BASE_URL))
        client._session = WebSession(cookies=httpx.Cookies(), context_id="11111111")
        client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BASE_URL
        )
        return client

    def _capture(self, seen, legal=True):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/JSON-RPC":
                # The endpoint answers true when the range is NOT legal.
                return httpx.Response(200, json={"result": not legal, "id": 1})
            seen.append(request.url)
            return httpx.Response(200, content=ARCHIVE)

        return handler

    async def test_sends_the_parameters_the_ui_sends(self, tmp_path):
        import datetime

        seen: list[httpx.URL] = []
        await export_saft_web(
            self._web(self._capture(seen)),
            datetime.date(2026, 1, 1), datetime.date(2026, 6, 30),
            tmp_path / "s.zip", version="1.3",
        )

        assert seen[0].path == "/execute/saftExport"
        assert seen[0].params["act"] == "downloadSAFTZipfile"
        assert seen[0].params["from"] == "2026-01-01"
        assert seen[0].params["to"] == "2026-06-30"
        assert seen[0].params["version"] == "1.3"
        assert seen[0].params["split"] == "false"
        assert seen[0].params["contextId"] == "11111111"

    async def test_version_13_is_the_default(self, tmp_path):
        """1.30 is the version mandatory from 2025-01-01, so a caller who does
        not think to ask must not be handed a 1.2 file. Both routes default to
        it — the token one since API 2.75.10."""
        import datetime

        seen: list[httpx.URL] = []
        await export_saft_web(
            self._web(self._capture(seen)),
            datetime.date(2026, 1, 1), datetime.date(2026, 12, 31), tmp_path,
        )

        assert seen[0].params["version"] == "1.3"

    async def test_an_unknown_version_is_refused_before_any_request(self, tmp_path):
        import datetime

        seen: list[httpx.URL] = []
        with pytest.raises(ValueError, match="version must be"):
            await export_saft_web(
                self._web(self._capture(seen)),
                datetime.date(2026, 1, 1), datetime.date(2026, 12, 31),
                tmp_path, version="1.4",
            )
        assert seen == []

    async def test_a_multi_year_range_is_refused(self, tmp_path):
        """`validation_saft_export_multiple_years_not_allowed` — asked of the
        server rather than reimplemented, since we only half know the rule."""
        import datetime

        seen: list[httpx.URL] = []
        with pytest.raises(ValueError, match="may not span calendar years"):
            await export_saft_web(
                self._web(self._capture(seen, legal=False)),
                datetime.date(2025, 1, 1), datetime.date(2026, 12, 31), tmp_path,
            )
        assert seen == []

    async def test_inbox_delivery_writes_nothing_locally(self, tmp_path):
        """The file goes to bilagsmottak, so there is no download to return."""
        import datetime

        seen: list[httpx.URL] = []
        out = await export_saft_web(
            self._web(self._capture(seen)),
            datetime.date(2026, 1, 1), datetime.date(2026, 3, 31), tmp_path,
            send_to_inbox_archive=True,
        )

        assert out is None
        assert list(tmp_path.iterdir()) == []

    async def test_directory_destination_names_by_range_and_version(self, tmp_path):
        import datetime

        out = await export_saft_web(
            self._web(self._capture([])),
            datetime.date(2026, 1, 1), datetime.date(2026, 6, 30),
            tmp_path, version="1.2",
        )

        assert out.name == "saft-2026-01-01-2026-06-30-v1.2.zip"

    async def test_needs_a_web_session(self, tmp_path):
        import datetime
        from tripletex.session import WebSessionRequired

        with pytest.raises(WebSessionRequired):
            await export_saft_web(
                _client(), datetime.date(2026, 1, 1), datetime.date(2026, 3, 31),
                tmp_path,
            )
