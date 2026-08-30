"""Voucher enumeration, metadata, and document download."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from tripletex.endpoints._paging import paginate
from tripletex.models import VoucherMeta
from tripletex.session import require_web_session

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

_VOUCHER_FIELDS = "id,number,tempNumber,year,date,description,attachment(id,fileName)"


def _to_meta(v: dict) -> VoucherMeta:
    attachment = v.get("attachment")
    doc_ids = [attachment["id"]] if attachment and attachment.get("id") else []
    return VoucherMeta(
        id=v["id"],
        number=v.get("number"),
        temp_number=v.get("tempNumber"),
        year=v.get("year"),
        date=v.get("date"),
        description=v.get("description"),
        document_ids=doc_ids,
    )


async def list_vouchers(
    client: TripletexClient,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int | None = None,
) -> list[VoucherMeta]:
    """Enumerate vouchers with attachment info via the JSON API.

    GET /v2/ledger/voucher?dateFrom=X&dateTo=Y&fields=...&from=0&count=N
    """
    params: dict[str, str] = {"fields": _VOUCHER_FIELDS}
    if date_from:
        params["dateFrom"] = date_from.isoformat()
    if date_to:
        params["dateTo"] = date_to.isoformat()

    values = await paginate(client, "/v2/ledger/voucher", params=params, limit=limit)
    return [_to_meta(v) for v in values]


async def list_non_posted_vouchers(
    client: TripletexClient,
    date_from: date | None = None,
    date_to: date | None = None,
    changed_since: str | None = None,
    include_non_approved: bool = True,
) -> list[VoucherMeta]:
    """Vouchers registered but not yet posted — the "to process" queue.

    GET /v2/ledger/voucher/>nonPosted. Works with API-token auth, unlike the
    voucher inbox.

    Quirks, all measured: the endpoint ignores `from` and `count` and returns
    the whole set every time (`fullResultSize` is 0), so `changed_since` is how
    you fetch incrementally — it wants strict `YYYY-MM-DDThh:mm:ssZ` and rejects
    a bare date with 422. `include_non_approved` only narrows the result when
    the voucher approval workflow is enabled; with it off both values return the
    same set.
    """
    params: dict[str, str] = {
        "includeNonApproved": "true" if include_non_approved else "false",
        "fields": _VOUCHER_FIELDS,
    }
    if date_from:
        params["dateFrom"] = date_from.isoformat()
    if date_to:
        params["dateTo"] = date_to.isoformat()
    if changed_since:
        params["changedSince"] = changed_since

    data = await client.get_json("/v2/ledger/voucher/>nonPosted", params=params)
    return [_to_meta(v) for v in data.get("values", [])]


async def download_voucher_document(
    client: TripletexClient,
    document_id: int,
    dest: Path,
) -> Path:
    """Download a voucher document (PDF/image).

    GET /execute/document?act=view&id=X&contextId=Y
    """
    session = require_web_session(client.session, "Downloading voucher documents")
    context_id = session.context_id
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
