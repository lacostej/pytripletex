"""The company document archive — the one feature with no API at all.

The listing is scraped, so these tests care most about what happens when the
scrape goes wrong. A parser that returns `[]` for a page it failed to understand
is indistinguishable from an empty archive, and an audit pack built on that would
silently omit every document it exists to include.

The markup below is synthetic, modelled on the real page's structure. Real
archive listings are business records — folder and file names of actual leases,
agreements and invoices — and none of that belongs in a fixture.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.archive import (
    archive_tree,
    download_archive_document,
    find_in_archive,
    list_archive,
    walk_archive,
)
from tripletex.parsers.html import ArchiveListingUnparseable, parse_archive_listing
from tripletex.session import ApiSession, WebSession, WebSessionRequired

BASE_URL = "https://tripletex.no"


def _row(idx: int, entry_id: int, revision: int, name: str, folder: bool,
         size: str = "", archive_date: str = "") -> str:
    icon = "archive-icon__folder" if folder else "archive-icon__file"
    ligature = "folder" if folder else "insert_drive_file"
    link = (f"<a onclick=\"javascript:tlxGetScope(this).navigate('{entry_id}')\">{name}</a>"
            if folder else
            f'<a class="linkFunction" href="javascript:extraFrame.viewerDocument({entry_id})">{name}</a>')
    return f"""<tr>
      <td class="select">
        <input type="hidden" name="documentsAndFolders[{idx}].id" value="{entry_id}"/>
        <input type="hidden" name="documentsAndFolders[{idx}].revision" value="{revision}"/>
      </td>
      <td class="table-cell--status">
        <i class="material-icons icon--with-text {icon}">{ligature}</i>
        <span class="text--with-icon">{link}</span>
      </td>
      <td class="table-cell--text">{archive_date}</td>
      <td class="right table-cell--text-small">{size}</td>
    </tr>"""


def _page(*rows: str) -> str:
    header = '<tr><th class="select"></th><th>Name</th><th>Archive date</th><th>Size</th></tr>'
    return f"<table>{header}{''.join(rows)}</table>"


ROOT = _page(
    _row(0, 818231093, 1, "Avtaler", folder=True),
    _row(1, 841163559, 3, "Innskudd", folder=True),
    _row(2, 532701422, 2, "Firmaattest.pdf", folder=False, size="28,4 KB",
         archive_date="2023-10-15"),
)
AVTALER = _page(_row(0, 900000001, 1, "Leiekontrakt.pdf", folder=False, size="1,2 MB"))
INNSKUDD = _page(_row(0, 900000002, 1, "Deposit.pdf", folder=False, size="216,1 KB"))


def _web(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = WebSession(cookies=httpx.Cookies(), context_id="1")
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _api(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _serving(seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url)
        folder = request.url.params.get("folderId")
        body = {None: ROOT, "818231093": AVTALER, "841163559": INNSKUDD}.get(folder, _page())
        return httpx.Response(200, text=body)

    return handler


class TestParsing:
    def test_reads_id_revision_name_and_kind(self):
        entries = parse_archive_listing(ROOT)

        assert [(e.id, e.revision, e.name, e.is_folder) for e in entries] == [
            (818231093, 1, "Avtaler", True),
            (841163559, 3, "Innskudd", True),
            (532701422, 2, "Firmaattest.pdf", False),
        ]

    def test_revision_is_kept_because_writes_need_it(self):
        """Every write action must send the revision it saw, and the listing is
        the only place to get it."""
        innskudd = parse_archive_listing(ROOT)[1]

        assert innskudd.revision == 3

    def test_size_and_archive_date_where_present(self):
        doc = parse_archive_listing(ROOT)[2]

        assert doc.size_text == "28,4 KB"
        assert doc.archive_date == datetime.date(2023, 10, 15)

    def test_folders_carry_neither(self):
        folder = parse_archive_listing(ROOT)[0]

        assert folder.size_text is None
        assert folder.archive_date is None

    def test_an_empty_folder_is_empty_not_an_error(self):
        assert parse_archive_listing(_page()) == []

    def test_an_empty_folder_with_chrome_rows_is_still_empty(self):
        """A real folder page carries a title row and a totals row, both using
        <td>. An empty folder is exactly those two and no entries — caught as a
        false alarm the first time this ran against live data, because the guard
        was keyed on having a <td> rather than on the hidden inputs an entry
        row carries."""
        chrome = _page(
            '<tr><td colspan="7">Document archive</td></tr>',
            '<tr><td colspan="7">Total 0 B</td></tr>',
        )

        assert parse_archive_listing(chrome) == []

    def test_changed_markup_raises_rather_than_returning_nothing(self):
        """The failure this guards: Tripletex restyles the page, the selectors
        stop matching, and an audit pack quietly contains no documents."""
        restyled = ROOT.replace("archive-icon__folder", "icon-folder-v2").replace(
            "archive-icon__file", "icon-file-v2"
        )

        with pytest.raises(ArchiveListingUnparseable, match="markup"):
            parse_archive_listing(restyled)

    def test_a_renamed_prefix_is_survivable(self):
        """The row selector matches on the `.id` suffix, not the whole name, so
        Tripletex renaming the form collection does not break it."""
        renamed = ROOT.replace("documentsAndFolders[", "archiveItems[")

        assert len(parse_archive_listing(renamed)) == 3

    def test_a_renamed_id_field_raises_rather_than_yielding_nothing(self):
        """What does break it: the id input itself being renamed. Every row then
        fails the first check and the parser walks off the end with an empty
        list. The page plainly still has rows, so that must be an error — an
        empty archive and an unreadable one are not the same answer."""
        renamed = ROOT.replace('.id" value=', '.identifier" value=')

        with pytest.raises(ArchiveListingUnparseable, match="entry-shaped"):
            parse_archive_listing(renamed)

    def test_a_row_missing_its_revision_raises(self):
        broken = ROOT.replace('name="documentsAndFolders[1].revision" value="3"', 'name="x"')

        with pytest.raises(ArchiveListingUnparseable):
            parse_archive_listing(broken)

    def test_the_error_says_how_much_it_did_read(self):
        broken = ROOT.replace('name="documentsAndFolders[1].revision" value="3"', 'name="x"')

        with pytest.raises(ArchiveListingUnparseable, match="Read 2 row"):
            parse_archive_listing(broken)


class TestListing:
    async def test_lists_the_root(self):
        entries = await list_archive(_web(_serving()))

        assert [e.name for e in entries] == ["Avtaler", "Innskudd", "Firmaattest.pdf"]

    async def test_folder_id_is_sent(self):
        seen: list[httpx.URL] = []

        await list_archive(_web(_serving(seen)), 818231093)

        assert seen[0].params["folderId"] == "818231093"
        assert seen[0].params["act"] == "content"

    async def test_root_sends_no_folder_id(self):
        seen: list[httpx.URL] = []

        await list_archive(_web(_serving(seen)))

        assert "folderId" not in seen[0].params

    async def test_api_token_is_refused(self):
        with pytest.raises(WebSessionRequired):
            await list_archive(_api(_serving()))


class TestWalk:
    async def test_descends_into_folders(self):
        walked = await walk_archive(_web(_serving()))

        assert [e.name for _, e in walked] == [
            "Avtaler", "Leiekontrakt.pdf", "Innskudd", "Deposit.pdf", "Firmaattest.pdf",
        ]

    async def test_paths_record_where_each_entry_sits(self):
        walked = await walk_archive(_web(_serving()))
        by_name = {e.name: p for p, e in walked}

        assert by_name["Leiekontrakt.pdf"] == ("Avtaler",)
        assert by_name["Deposit.pdf"] == ("Innskudd",)
        assert by_name["Firmaattest.pdf"] == ()

    async def test_one_request_per_folder(self):
        seen: list[httpx.URL] = []

        await walk_archive(_web(_serving(seen)))

        assert len(seen) == 3, "root + two folders"

    async def test_find_matches_an_agreed_path(self):
        """The point of an agreed structure — ask whether what should be filed
        somewhere actually is."""
        walked = await walk_archive(_web(_serving()))

        assert [e.name for e in find_in_archive(walked, "Avtaler")] == ["Leiekontrakt.pdf"]
        assert find_in_archive(walked, "Nope") == []

    async def test_runaway_nesting_raises(self):
        """A folder that contains itself would otherwise loop until the session
        died. Twelve levels is a filing structure; more is a fault."""

        def endless(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_page(_row(0, 1, 1, "Loop", folder=True)))

        with pytest.raises(RuntimeError, match="nesting exceeded"):
            await walk_archive(_web(endless))

    async def test_tree_rendering_indents_by_depth(self):
        walked = await walk_archive(_web(_serving()))

        rendered = archive_tree(walked)
        assert "Avtaler/" in rendered
        assert "    Leiekontrakt.pdf  (1,2 MB)" in rendered


class TestDownload:
    def _docs(self, seen: list | None = None):
        def handler(request: httpx.Request) -> httpx.Response:
            if seen is not None:
                seen.append(request.url.path)
            if request.url.path.endswith("/content"):
                return httpx.Response(200, content=b"%PDF-1.4 stub")
            return httpx.Response(200, json={"value": {
                "id": 579734164, "fileName": "Firmaattest.pdf",
                "size": 29040, "mimeType": "application/pdf"}})

        return handler

    async def test_a_token_is_enough(self, tmp_path: Path):
        """The listing needs a browser session; fetching a known id does not.
        That is what makes an archive-backed pack schedulable."""
        got = await download_archive_document(_api(self._docs()), 579734164, tmp_path)

        assert got.name == "Firmaattest.pdf"
        assert got.read_bytes().startswith(b"%PDF")

    async def test_a_directory_uses_the_documents_own_name(self, tmp_path: Path):
        seen: list[str] = []

        await download_archive_document(_api(self._docs(seen)), 579734164, tmp_path)

        assert "/v2/document/579734164" in seen, "the name costs one lookup"

    async def test_an_explicit_path_skips_the_lookup(self, tmp_path: Path):
        seen: list[str] = []

        got = await download_archive_document(
            _api(self._docs(seen)), 579734164, tmp_path / "chosen.pdf"
        )

        assert got.name == "chosen.pdf"
        assert seen == ["/v2/document/579734164/content"]
