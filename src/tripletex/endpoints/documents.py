"""Documents reception — files waiting for an employee.

A third queue, distinct from the two in `vouchers.py`. Vouchers in reception
(bilagsmottak) are accounting documents awaiting coding; these are plain files
mailed or uploaded to an employee's document inbox, carrying no voucher,
supplier or amount.

Reachable with API tokens on every company, including those without the API_V2
module, which is unusual — most of the interesting queues are not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tripletex.endpoints._paging import paginate
from tripletex.models import DocumentReceptionContext, DocumentReceptionItem

if TYPE_CHECKING:
    from pathlib import Path

    from tripletex.client import TripletexClient

DOCUMENT_RECEPTION = "/v2/documentReception"

# The order the interface itself asks for: newest first.
_SORT = {"sortBy": "CREATED_DATE", "sortDirection": "DESCENDING"}


async def list_document_reception(
    client: TripletexClient,
    limit: int | None = None,
) -> list[DocumentReceptionItem]:
    """List the documents waiting in reception, newest first.

    GET /v2/documentReception — undocumented, and found in the interface's own
    traffic, but it answers token auth on every company.

    Unlike `>voucherReception` this endpoint honours `from`/`count`, so ordinary
    paging applies. `fullResultSize` is *not* a total here — it reports
    `from + len(values)`, plus one when the page came back full, so it can exceed
    the real count and is not monotonic. `paginate()` never reads it.
    """
    return [
        DocumentReceptionItem.model_validate(row)
        for row in await paginate(
            client, DOCUMENT_RECEPTION, params=dict(_SORT), limit=limit
        )
    ]


async def get_document_reception_context(
    client: TripletexClient,
) -> DocumentReceptionContext:
    """Fetch the ingest address and the caller's rights over this queue.

    GET /v2/documentReception/pageContext

    Worth calling alongside the list: `auth_all_employees` is what separates
    "the queue is empty" from "my part of it is", and an empty queue read
    without it is the silent-filtering trap in `adapter-notes.md`.
    """
    data = await client.get_json(f"{DOCUMENT_RECEPTION}/pageContext")
    return DocumentReceptionContext.model_validate(data["value"])


async def download_document(
    client: TripletexClient,
    document_id: int,
    dest: Path,
) -> Path:
    """Download a document's bytes to `dest`.

    GET /v2/document/{id}/content — works with API tokens as well as a web
    session.

    Do not be tempted to send a specific `Accept`: naming the media type is what
    makes this endpoint 400, including when you name it correctly
    (`application/pdf` for a PDF, `image/jpeg` for a JPEG). `client.download()`
    sends `Accept: */*`, which is what works.
    """
    return await client.download(
        f"/v2/document/{document_id}/content", params={}, dest=dest
    )
