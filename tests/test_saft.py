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
from tripletex.endpoints.saft import audit_file_version, export_saft
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

    async def test_a_directory_destination_is_named_by_year(self, tmp_path):
        out = await export_saft(_client(), 2025, tmp_path)

        assert out.name == "saft-2025.zip"
        assert out.parent == tmp_path

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
