"""Voucher enumeration, metadata, and document download."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from tripletex.models import VoucherMeta

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

_VOUCHER_FIELDS = "id,number,year,date,description,attachment(id,fileName)"


async def list_vouchers(
    client: TripletexClient,
    date_from: date | None = None,
    date_to: date | None = None,
    count: int = 1000,
) -> list[VoucherMeta]:
    """Enumerate vouchers with attachment info via the JSON API.

    GET /v2/ledger/voucher?dateFrom=X&dateTo=Y&fields=...&from=0&count=N
    """
    params: dict[str, str] = {
        "from": "0",
        "count": str(count),
        "fields": _VOUCHER_FIELDS,
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
            attachment = v.get("attachment")
            doc_ids = [attachment["id"]] if attachment and attachment.get("id") else []
            doc_filename = attachment.get("fileName") if attachment else None

            all_vouchers.append(
                VoucherMeta(
                    id=v["id"],
                    number=v.get("number"),
                    year=v.get("year"),
                    date=v.get("date"),
                    description=v.get("description"),
                    document_ids=doc_ids,
                )
            )

        total = data.get("fullResultSize", len(all_vouchers))
        offset += len(values)
        if offset >= total:
            break

    return all_vouchers


async def download_voucher_document(
    client: TripletexClient,
    document_id: int,
    dest: Path,
) -> Path:
    """Download a voucher document (PDF/image).

    GET /execute/document?act=view&id=X&contextId=Y
    """
    context_id = client.session.context_id
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
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Enumerating vouchers...")
    vouchers = await list_vouchers(client, date_from, date_to)
    logger.info("Found %d vouchers", len(vouchers))

    downloaded = 0
    skipped = 0

    for i, voucher in enumerate(vouchers):
        year = voucher.year or "unknown"
        voucher_dir = dest_dir / str(year) / f"voucher_{voucher.number or voucher.id}"
        voucher_dir.mkdir(parents=True, exist_ok=True)

        # Save metadata
        meta_path = voucher_dir / "meta.json"
        meta_path.write_text(voucher.model_dump_json(indent=2))

        # Download documents
        for doc_id in voucher.document_ids:
            doc_path = voucher_dir / f"document_{doc_id}.pdf"
            if doc_path.exists():
                skipped += 1
                continue
            try:
                await download_voucher_document(client, doc_id, doc_path)
                downloaded += 1
                if (downloaded % 10) == 0:
                    logger.info(
                        "[%d/%d] Downloaded %d documents so far...",
                        i + 1, len(vouchers), downloaded,
                    )
            except Exception as e:
                logger.warning("Failed to download document %d: %s", doc_id, e)

            await asyncio.sleep(delay)

    # Save index
    index_path = dest_dir / "vouchers.json"
    index_data = [v.model_dump(mode="json") for v in vouchers]
    index_path.write_text(json.dumps(index_data, indent=2, default=str))
    logger.info(
        "Done: %d vouchers, %d documents downloaded, %d skipped",
        len(vouchers), downloaded, skipped,
    )

    return vouchers
