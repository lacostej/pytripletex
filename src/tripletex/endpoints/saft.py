"""SAF-T export — the standard audit file Skatteetaten asks for.

`GET /v2/saft/exportSAFT?year=YYYY` is documented, marked `[BETA]`, and works
with an API token, so unlike most of the reporting surface this is schedulable.

**It is a ZIP.** The response is a zip archive containing one `.xml`, despite the
inner file and every manual export being named `.xml`. Anything pointed at a
bare XML path needs the extra step.

**The token route can emit 1.3, as of 2026-09-11.** This is new and it matters:
`version` was absent from the endpoint when this module was written, which is
why `export_saft_web` exists at all. It appears in `/v2/openapi.json` at API
2.75.10 — `SAF-T schema version to export: "1.2" (default) or "1.3", "1.4" is
planned` — and is *not* in `/v2/swagger.json`, which is frozen at 2.71.30 and
still describes `year` as the only argument.

Verified against a live token on 2026-09-11, Bonita Handel FY2025:

    version=1.3   1,506,602 b   AuditFileVersion 1.30
    version=1.2   1,490,559 b   AuditFileVersion 1.20
    (omitted)     1,490,560 b   AuditFileVersion 1.20

So **1.3 no longer needs a web session**, and a compliant export is schedulable.
`export_saft_web` is kept for the one thing the token route still cannot do —
an arbitrary date range inside a year — not for the version.

Omitting `version` still yields 1.20, so older callers are unaffected; an empty
or unknown value is a `422` rather than a silent fallback.

1.2 and 1.3 are different documents, not the same file relabelled — the account
element swaps `StandardAccountID` for `GroupingCategory` + `GroupingCode`. Use
`audit_file_version()` on anything you did not just generate, and check which
version the recipient requires.

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

#: What the endpoint emits when `version` is omitted, measured 2026-09-11. Not a
#: promise from Tripletex — read it off the file with `audit_file_version()`
#: rather than trusting this constant.
DEFAULT_AUDIT_FILE_VERSION = "1.20"

#: Versions both routes offer. Requested as "1.2"/"1.3"; reported inside the file
#: as "1.20"/"1.30". The token route accepts these too as of API 2.75.10, so this
#: is no longer a web-only concern.
SAFT_VERSIONS = ("1.2", "1.3")


async def export_saft(
    client: TripletexClient,
    year: int,
    dest: Path | str,
    version: str = "1.3",
    extract: bool = False,
) -> Path:
    """Download the SAF-T export for `year`. Returns the path written.

    GET /v2/saft/exportSAFT, **API token** — no web session. Streams to `dest`
    rather than buffering, because the archive expands roughly twenty-five-fold
    and a full year of a busy company is not something to hold in memory.

    **`version` defaults to 1.3, not to Tripletex's default of 1.2.** 1.30 is the
    version mandatory for periods from 2025-01-01, so defaulting to the
    endpoint's own 1.2 would hand back a non-compliant file to a caller who did
    not think to ask. Pass `version="1.2"` explicitly for an older period.

    Note this is a behaviour change: before API 2.75.10 the endpoint had no
    `version` argument and always produced 1.20.

    `dest` may be a directory, in which case the file is written as
    `saft-<year>-v<version>.zip` inside it — the version is in the name because
    1.2 and 1.3 are different documents, and two exports of one year must not
    overwrite each other. Repeated calls to the same path do overwrite.

    Set `extract` to unpack the single XML beside the archive and return that
    path instead. Off by default: the zip is what the endpoint gives, and
    keeping it is the honest artifact to archive.
    """
    if version not in SAFT_VERSIONS:
        raise ValueError(f"version must be one of {SAFT_VERSIONS}, not {version!r}")

    dest = Path(dest)

    if dest.is_dir() or not dest.suffix:
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f"saft-{year}-v{version}.zip"
    else:
        target = dest

    logger.info("Exporting SAF-T %s for %s", version, year)
    await client.download(
        "/v2/saft/exportSAFT", {"year": str(year), "version": version}, target
    )

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


# --- The web export: arbitrary date ranges, which the token route cannot do ---

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
    read off `/saftExport.js`. It offers two things the documented API does not:

    - **an arbitrary date range** rather than a whole year;
    - delivery into bilagsmottak, and splitting for very large exports.

    It is **no longer needed for version 1.3**: `export_saft` reaches that with a
    token as of API 2.75.10. Prefer the token route unless you need a range that
    is not a whole year — it needs no browser session, so it can be scheduled.

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
