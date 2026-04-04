"""Voucher inbox endpoints — unprocessed receipts/invoices."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


class InboxDocument(BaseModel):
    id: int
    title: Optional[str] = None
    mime_type: Optional[str] = Field(default=None, alias="mimeType")
    size: Optional[int] = None

    model_config = {"populate_by_name": True, "extra": "allow"}


class InboxItem(BaseModel):
    """An item in the voucher inbox."""
    id: int
    description: Optional[str] = None
    filter_type: Optional[str] = Field(default=None, alias="filterType")
    received_date: Optional[datetime.datetime] = Field(default=None, alias="receivedDate")
    voucher_id: Optional[int] = Field(default=None, alias="voucherId")
    invoice_number: Optional[str] = Field(default=None, alias="invoiceNumber")
    invoice_date: Optional[datetime.date] = Field(default=None, alias="invoiceDate")
    due_date: Optional[datetime.date] = Field(default=None, alias="dueDate")
    is_due: Optional[bool] = Field(default=None, alias="isDue")
    supplier_name: Optional[str] = Field(default=None, alias="supplierName")
    invoice_amount: Optional[Decimal] = Field(default=None, alias="invoiceAmount")
    invoice_currency: Optional[str] = Field(default=None, alias="invoiceCurrency")
    filename: Optional[str] = None
    sender_email_address: Optional[str] = Field(default=None, alias="senderEmailAddress")
    voucher_documents: Optional[list[InboxDocument]] = Field(default=None, alias="voucherDocuments")
    comment_count: int = Field(default=0, alias="commentCount")
    is_locked: bool = Field(default=False, alias="isLocked")

    model_config = {"populate_by_name": True, "extra": "allow"}


async def list_inbox(
    client: TripletexClient,
    count: int = 1000,
    sort_direction: str = "DESCENDING",
) -> list[InboxItem]:
    """List items in the voucher inbox.

    GET /v2/voucherInbox/inboxFiltered
    """
    all_items: list[InboxItem] = []
    offset = 0

    while True:
        data = await client.get_json(
            "/v2/voucherInbox/inboxFiltered",
            params={
                "from": str(offset),
                "count": str(min(count - offset, 50)),
                "sortDirection": sort_direction,
            },
        )

        values = data.get("values", [])
        if not values:
            break

        all_items.extend(InboxItem.model_validate(v) for v in values)

        total = data.get("fullResultSize", len(all_items))
        offset += len(values)
        if offset >= total or offset >= count:
            break

    return all_items
