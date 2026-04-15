"""Invoice endpoints (official API)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from tripletex.models import Invoice

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def list_invoices(
    client: TripletexClient,
    invoice_date_from: date,
    invoice_date_to: date,
    fields: str = "",
    count: int = 1000,
) -> list[Invoice]:
    """GET /v2/invoice"""
    params: dict[str, str] = {
        "invoiceDateFrom": invoice_date_from.isoformat(),
        "invoiceDateTo": invoice_date_to.isoformat(),
        "from": "0",
        "count": str(count),
    }
    if fields:
        params["fields"] = fields
    data = await client.get_json("/v2/invoice", params=params)
    return [Invoice.model_validate(v) for v in data.get("values", [])]


async def get_invoice(
    client: TripletexClient,
    invoice_id: int,
    fields: str = "",
) -> Invoice:
    """GET /v2/invoice/{id}"""
    params = {"fields": fields} if fields else {}
    data = await client.get_json(f"/v2/invoice/{invoice_id}", params=params)
    return Invoice.model_validate(data.get("value", data))


async def create_invoice(
    client: TripletexClient,
    payload: dict[str, Any],
) -> Invoice:
    """POST /v2/invoice"""
    data = await client.post_json("/v2/invoice", json_body=payload)
    return Invoice.model_validate(data.get("value", data))
