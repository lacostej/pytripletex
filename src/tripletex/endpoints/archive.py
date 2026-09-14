"""The company document archive — folders, and the documents in them.

**This feature has no API.** Not an undocumented one: none at all. Checked
2026-09-14 against both published specifications, re-fetched and searched as raw
text — `/v2/swagger.json` (2.71.30) and `/v2/openapi.json` (2.75.10) contain no
`folder`, no `createFolder`, no `parentFolder`, and no `ArchiveForm`. The only
`archiveObjectType` in either belongs to `ArchiveRelation`, a schema no operation
references, reachable only as a nested field on `Order`.

What is documented is the *entity-scoped* archive — `/v2/documentArchive/
{account,customer,employee,product,project,prospect,supplier}/{id}` — which hangs
documents off a business object. That is a different feature from the company
archive with user-defined folders that the UI presents, and it cannot express it.

So the listing here is **scraped**, and reading it is a deliberate trade:

    list_archive / walk_archive     web session, HTML scraping, brittle
    download_archive_document       API token, documented, stable

That split is the useful part. Discovery needs a browser session and can break
when Tripletex restyles a page; *fetching a document you already have the id of*
is `GET /v2/document/{id}/content`, which is documented, token-reachable and
therefore schedulable. A pipeline can enumerate rarely and fetch often.

**Document ids are company-scoped.** Measured: id 579734164 answers 200 under a
Bonita Services token and 404 under Bonita Handel's. An id alone is not a
reference; it needs the company with it.

**Deletion is soft by default.** The UI's delete leaves a document fetchable by
id — measured, a "deleted" document still returned its full 116 KB under a token
— until it is removed again from the deleted-items view. An id you recorded may
therefore outlive the document's apparent removal.

The write actions are `no.tripletex.tcp.web.ArchiveForm` over JSON-RPC —
`doCreateFolder`, `doCompleteUpload`, `doDelete`, `doDeletePermanently`,
`doMoveToFolder` — and are deliberately not implemented here. Reading is what an
audit pack needs, and each write would need its entry's current `revision`,
which only the scraped listing supplies.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from tripletex.models import ArchiveEntry
from tripletex.parsers.html import parse_archive_listing
from tripletex.session import require_web_session

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

#: The archive page's own parameters. `scope=ajaxContent` asks for the table
#: fragment rather than the whole page — 46 KB instead of a full chrome render.
_LISTING = {"viewMode": "0", "act": "content", "scope": "ajaxContent"}

#: How deep `walk_archive` will go before deciding something is wrong. The
#: archive is a hand-made filing structure, not a data set; a dozen levels means
#: a cycle or a parser fault, not a deeply-nested company.
MAX_DEPTH = 12


async def list_archive(
    client: TripletexClient, folder_id: int | None = None
) -> list[ArchiveEntry]:
    """One level of the archive. **Web session only.**

    GET /execute/archive — an HTML fragment, parsed. Pass `folder_id` to list
    inside a folder; omit it for the root.

    Raises `ArchiveListingUnparseable` rather than returning `[]` when the page
    cannot be read, because an empty archive and an unreadable one are
    indistinguishable to the caller and only one of them is safe.
    """
    require_web_session(client.session, "The document archive")

    params = dict(_LISTING)
    if folder_id is not None:
        params["folderId"] = str(folder_id)

    html = await client.get_html("/execute/archive", params)
    entries = parse_archive_listing(html)
    logger.debug(
        "Archive folder %s: %d entries", folder_id if folder_id else "(root)", len(entries)
    )
    return entries


async def walk_archive(
    client: TripletexClient,
    folder_id: int | None = None,
    _prefix: tuple[str, ...] = (),
    _depth: int = 0,
) -> list[tuple[tuple[str, ...], ArchiveEntry]]:
    """Every entry beneath `folder_id`, depth first, with its path.

    Returns `(path, entry)` pairs where `path` is the folder names above the
    entry — `("Avtaler", "Leiekontrakter")` — so a caller can match against an
    agreed structure without tracking ids itself.

    One request per folder: the page lists a single level, so a tree of *n*
    folders costs *n + 1* requests. Cheap for a filing structure, and worth
    remembering before pointing it at anything larger.
    """
    if _depth > MAX_DEPTH:
        raise RuntimeError(
            f"Archive nesting exceeded {MAX_DEPTH} levels at {'/'.join(_prefix)!r}. "
            f"That is more likely a cycle or a parser fault than a real structure."
        )

    found: list[tuple[tuple[str, ...], ArchiveEntry]] = []
    for entry in await list_archive(client, folder_id):
        found.append((_prefix, entry))
        if entry.is_folder:
            found.extend(
                await walk_archive(client, entry.id, _prefix + (entry.name,), _depth + 1)
            )
    return found


def archive_tree(walked: list[tuple[tuple[str, ...], ArchiveEntry]]) -> str:
    """Render `walk_archive`'s output as an indented tree, for eyeballing.

    Kept separate from the walk so the data stays data — a caller reconciling
    against an expected structure wants the pairs, not a string.
    """
    lines = []
    for path, entry in walked:
        mark = "/" if entry.is_folder else ""
        size = f"  ({entry.size_text})" if entry.size_text else ""
        lines.append(f"{'    ' * len(path)}{entry.name}{mark}{size}")
    return "\n".join(lines)


async def download_archive_document(
    client: TripletexClient, document_id: int, dest: Path | str
) -> Path:
    """Fetch one document by id. **API token is enough** — no web session.

    GET /v2/document/{id}/content, documented and stable, unlike the listing.

    `dest` may be a directory, in which case the document's own `fileName` is
    used. That name comes from `GET /v2/document/{id}`, so a directory
    destination costs one extra request; pass a full path to skip it.

    Raises `httpx.HTTPStatusError` with a 404 if the id does not exist **in this
    company** — ids are company-scoped, and the same id may well be a different
    document, or no document, elsewhere.
    """
    dest = Path(dest)

    if dest.is_dir() or not dest.suffix:
        meta = await client.get_json(f"/v2/document/{document_id}", {})
        name = (meta.get("value") or {}).get("fileName") or f"document_{document_id}"
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / name
    else:
        target = dest

    await client.download(f"/v2/document/{document_id}/content", {}, target)
    return target


def find_in_archive(
    walked: list[tuple[tuple[str, ...], ArchiveEntry]], *path: str
) -> list[ArchiveEntry]:
    """Entries filed at an exact folder path — `find_in_archive(t, "Avtaler")`.

    The point of an agreed structure: a caller states where a document *should*
    be and compares. Returns the entries directly inside that path, folders
    included; an empty list means the path holds nothing, while a path that does
    not exist at all is equally empty — so check the folder exists separately if
    the difference matters.
    """
    return [e for p, e in walked if p == tuple(path)]
