"""SAF-T export — the standard audit file Skatteetaten asks for.

`GET /v2/saft/exportSAFT?year=YYYY` is documented, marked `[BETA]`, and works
with an API token, so unlike most of the reporting surface this is schedulable.

**It is a ZIP, and it is version 1.20.** Two things measured 2026-09-09 that the
specification does not say and that decide whether this is the artifact you
want:

- The response is a zip archive containing one `.xml`, despite the inner file
  and every manual export being named `.xml`. Anything pointed at a bare XML
  path needs the extra step.
- The header reads `AuditFileVersion 1.20`. Tripletex's *web* export warns about
  SAF-T codes invalid for **1.3** — on both companies, `8960`
  (`saftCode 89`) and `9999` (`saftCode NA`) — while still producing a file. The
  API export emits those same codes without comment.

Whether the web export therefore writes 1.3 where this writes 1.20 is
**unverified**: only the API side has been read. If it does, the two are
different versions of the standard rather than the same file by two routes, and
this endpoint has no way to ask for the newer one — `year` is its only argument.
Use `audit_file_version()` on both before assuming they are interchangeable, and
check which version the recipient requires.

It is also synchronous and expands considerably: one measured year of a small
company was 449 KB compressed and 11.4 MB of XML, and a larger company scales
with voucher volume. Hence streaming to disk and a generous timeout.
"""

from __future__ import annotations

import json
import logging
import zipfile
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from tripletex.session import require_web_session

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

#: What this endpoint was measured to emit, 2026-09-09. Not a promise from
#: Tripletex — read it off the file with `audit_file_version()` rather than
#: trusting this constant.
EXPORTED_AUDIT_FILE_VERSION = "1.20"


async def export_saft(
    client: TripletexClient,
    year: int,
    dest: Path | str,
    extract: bool = False,
) -> Path:
    """Download the SAF-T export for `year`. Returns the path written.

    GET /v2/saft/exportSAFT. Streams to `dest` rather than buffering, because
    the archive expands roughly twenty-five-fold and a full year of a busy
    company is not something to hold in memory.

    `dest` may be a directory, in which case the file is written as
    `saft-<year>.zip` inside it. Note that repeated calls to the same path
    overwrite: Tripletex stamps its own `Content-Disposition` filename with the
    request time, so if you want that distinction, pass an explicit path.

    Set `extract` to unpack the single XML beside the archive and return that
    path instead. Off by default: the zip is what the endpoint gives, and
    keeping it is the honest artifact to archive.

    **Read the module docstring before using this for a filing.** The export was
    measured at version 1.20, and whether that matches what the recipient wants
    is not something this call can tell you.
    """
    dest = Path(dest)

    if dest.is_dir() or not dest.suffix:
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f"saft-{year}.zip"
    else:
        target = dest

    logger.info("Exporting SAF-T for %s (version %s)", year, EXPORTED_AUDIT_FILE_VERSION)
    await client.download("/v2/saft/exportSAFT", {"year": str(year)}, target)

    if not extract:
        return target
    return _extract_single_xml(target)


def _extract_single_xml(archive: Path) -> Path:
    """Unpack the one XML in a SAF-T archive, beside it. Returns its path.

    Deliberately strict about there being exactly one: a SAF-T archive holding
    several files, or none, is not the shape this was written against, and
    silently picking the first would hide that.
    """
    with zipfile.ZipFile(archive) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if len(members) != 1:
            raise RuntimeError(
                f"Expected exactly one XML in {archive.name}, found {len(members)}: "
                f"{members}"
            )
        name = members[0]
        # Guard against a member path escaping the destination directory.
        extracted = archive.parent / Path(name).name
        with zf.open(name) as src, open(extracted, "wb") as out:
            while chunk := src.read(1 << 20):
                out.write(chunk)
    return extracted


def audit_file_version(path: Path | str) -> str | None:
    """Read `AuditFileVersion` out of a SAF-T file, zipped or not.

    The one field worth checking before submitting anything: it is how you tell
    a 1.20 export from a 1.3 one, and the two are not interchangeable.
    """
    import xml.etree.ElementTree as ET

    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not members:
                return None
            with zf.open(members[0]) as handle:
                return _first_version(ET.parse(handle).getroot())
    return _first_version(ET.parse(path).getroot())


def _first_version(root) -> str | None:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "AuditFileVersion":
            return (element.text or "").strip() or None
    return None


# --- The web export, which the token API cannot reach -----------------------

#: Versions the web export offers. The API emits 1.2 only — reported in the file
#: header as "1.20" — and takes no version argument, so 1.3 is web-session only.
SAFT_VERSIONS = ("1.2", "1.3")


async def saft_range_is_legal(
    client: TripletexClient, date_from: date, date_to: date
) -> bool:
    """Whether Tripletex will accept this range for an export.

    POST /JSON-RPC AnnualAccounts.isNotLegalStartAndEndDateForSaftExport, which
    the UI calls before every download. The known rule is that a range may not
    span calendar years — the UI's failure message is
    `validation_saft_export_multiple_years_not_allowed` — but asking the server
    beats encoding a rule we only half know.

    Note the endpoint's own name is inverted: it answers *true* when the range is
    **not** legal. This function returns the sane direction.
    """
    session = require_web_session(client.session, "SAF-T export")
    body = {
        "method": "AnnualAccounts.isNotLegalStartAndEndDateForSaftExport",
        "params": [date_from.isoformat(), date_to.isoformat()],
        # The JSON-RPC correlation id. Client-chosen and only needs to be unique
        # within a batch — nothing looks it up, and the browser simply counts.
        "id": 1,
    }
    response = await client._request(
        "POST",
        "/JSON-RPC",
        params={
            "method": "AnnualAccounts.isNotLegalStartAndEndDateForSaftExport",
            "contextId": session.context_id,
        },
        content=json.dumps(body),
        extra_headers={"Content-type": "text/plain"},
        for_json=False,
    )
    return not bool((response.json() or {}).get("result"))


async def export_saft_web(
    client: TripletexClient,
    date_from: date,
    date_to: date,
    dest: Path | str,
    version: str = "1.3",
    send_to_inbox_archive: bool = False,
    split: bool = False,
    validate_range: bool = True,
) -> Path | None:
    """Export SAF-T over a date range, at a chosen version. Web session only.

    GET /execute/saftExport?act=downloadSAFTZipfile — the same call the UI makes,
    read off `/saftExport.js`. It offers three things the documented API does not:

    - **an arbitrary date range** rather than a whole year;
    - **version 1.3**, which the token endpoint cannot produce at all;
    - delivery into bilagsmottak, and splitting for very large exports.

    `send_to_inbox_archive` changes what this returns: the file is delivered to
    the document inbox rather than to the caller, so nothing is written locally
    and the result is `None`.

    `split` asks Tripletex to break the export into one file per month if it
    exceeds 2 GB, so the archive may then hold several XML members — which is
    why `export_saft`'s extraction refuses anything but a single one.

    **The range may not span calendar years.** `validate_range` asks the server
    first, which is one extra request and the same check the UI performs; turn it
    off only if you have already validated.

    A chart of accounts carrying SAF-T codes a version rejects produces a
    *warning* in the UI, not a refusal — both version flags read valid with two
    such accounts present — so this does not attempt to pre-empt that.
    """
    session = require_web_session(client.session, "SAF-T export")
    if version not in SAFT_VERSIONS:
        raise ValueError(f"version must be one of {SAFT_VERSIONS}, not {version!r}")

    if validate_range and not await saft_range_is_legal(client, date_from, date_to):
        raise ValueError(
            f"Tripletex rejects {date_from}..{date_to} for SAF-T export — a range "
            "may not span calendar years"
        )

    params = {
        "act": "downloadSAFTZipfile",
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "sendToInboxArchive": "true" if send_to_inbox_archive else "false",
        "split": "true" if split else "false",
        "version": version,
        "contextId": session.context_id,
    }

    if send_to_inbox_archive:
        logger.info("Exporting SAF-T %s to the document inbox", version)
        await client._request("GET", "/execute/saftExport", params=params, for_json=False)
        return None

    dest = Path(dest)
    if dest.is_dir() or not dest.suffix:
        dest.mkdir(parents=True, exist_ok=True)
        dest = dest / f"saft-{date_from}-{date_to}-v{version}.zip"

    logger.info("Exporting SAF-T %s for %s..%s", version, date_from, date_to)
    await client.download("/execute/saftExport", params, dest)
    return dest
