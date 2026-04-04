"""Voucher enumeration, metadata, and document download."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from tripletex.models import VoucherMeta
from tripletex.parsers.html import extract_voucher_document_ids

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)


async def list_vouchers(
    client: TripletexClient,
    date_from: date | None = None,
    date_to: date | None = None,
    count: int = 1000,
) -> list[VoucherMeta]:
    """Enumerate vouchers via the internal web API.

    GET /v2/ledger/voucher?dateFrom=X&dateTo=Y&from=0&count=N
    """
    params: dict[str, str] = {
        "from": "0",
        "count": str(count),
    }
    if date_from:
        params["dateFrom"] = date_from.isoformat()
    if date_to:
        params["dateTo"] = date_to.isoformat()

    all_vouchers: list[VoucherMeta] = []
    offset = 0

    while True:
        params["from"] = str(offset)
        data = await client.get_json("/v2/ledger/voucher", params=params)

        values = data.get("values", [])
        if not values:
            break

        for v in values:
            all_vouchers.append(
                VoucherMeta(
                    id=v["id"],
                    number=v.get("number"),
                    year=v.get("year"),
                    date=v.get("date"),
                    description=v.get("description"),
                )
            )

        total = data.get("fullResultSize", len(all_vouchers))
        offset += len(values)
        if offset >= total:
            break

    return all_vouchers


async def get_voucher_document_ids(
    client: TripletexClient,
    voucher_id: int,
) -> list[int]:
    """Get document IDs for a voucher by parsing the voucher page HTML.

    GET /execute/incomingInvoiceMenu?voucherId=X&contextId=Y
    Parses links with viewerDocument(ID) pattern.
    """
    context_id = client.session.context_id
    html = await client.get_html(
        "/execute/incomingInvoiceMenu",
        params={"voucherId": str(voucher_id), "contextId": context_id},
    )
    return extract_voucher_document_ids(html)


async def download_voucher_document(
    client: TripletexClient,
    document_id: int,
    dest_dir: Path,
) -> Path:
    """Download a voucher document (PDF/image).

    GET /execute/document?act=view&id=X&contextId=Y
    """
    context_id = client.session.context_id
    dest = dest_dir / f"document_{document_id}.pdf"
    return await client.download(
        "/execute/document",
        params={"act": "view", "id": str(document_id), "contextId": context_id},
        dest=dest,
    )


async def backup_all_vouchers(
    client: TripletexClient,
    dest_dir: Path,
    date_from: date | None = None,
    date_to: date | None = None,
    delay: float = 0.3,
) -> list[VoucherMeta]:
    """Download all vouchers with their documents.

    Creates a directory structure:
      dest_dir/
        vouchers.json          # metadata index
        YYYY/
          voucher_NNNN/
            meta.json
            document_ID.pdf

    Skips already-downloaded documents for resume capability.
    """
    import json

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Enumerating vouchers...")
    vouchers = await list_vouchers(client, date_from, date_to)
    logger.info("Found %d vouchers", len(vouchers))

    for i, voucher in enumerate(vouchers):
        year = voucher.year or "unknown"
        voucher_dir = dest_dir / str(year) / f"voucher_{voucher.number or voucher.id}"
        voucher_dir.mkdir(parents=True, exist_ok=True)

        meta_path = voucher_dir / "meta.json"

        # Get document IDs
        try:
            doc_ids = await get_voucher_document_ids(client, voucher.id)
            voucher.document_ids = doc_ids
        except Exception as e:
            logger.warning("Failed to get document IDs for voucher %d: %s", voucher.id, e)
            doc_ids = []

        # Save metadata
        meta_path.write_text(voucher.model_dump_json(indent=2))

        # Download documents
        for doc_id in doc_ids:
            doc_path = voucher_dir / f"document_{doc_id}.pdf"
            if doc_path.exists():
                logger.debug("Skipping existing document %d", doc_id)
                continue
            try:
                await download_voucher_document(client, doc_id, voucher_dir)
                logger.info(
                    "[%d/%d] Downloaded voucher %s document %d",
                    i + 1, len(vouchers), voucher.number or voucher.id, doc_id,
                )
            except Exception as e:
                logger.warning("Failed to download document %d: %s", doc_id, e)

        await asyncio.sleep(delay)

    # Save index
    index_path = dest_dir / "vouchers.json"
    index_data = [v.model_dump(mode="json") for v in vouchers]
    index_path.write_text(json.dumps(index_data, indent=2, default=str))
    logger.info("Saved voucher index to %s", index_path)

    return vouchers
