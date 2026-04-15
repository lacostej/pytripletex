"""Order endpoints (official API)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from tripletex.models import Order, OrderLine

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def list_orders(
    client: TripletexClient,
    order_date_from: date,
    order_date_to: date,
    fields: str = "",
    count: int = 1000,
) -> list[Order]:
    """GET /v2/order"""
    params: dict[str, str] = {
        "orderDateFrom": order_date_from.isoformat(),
        "orderDateTo": order_date_to.isoformat(),
        "from": "0",
        "count": str(count),
    }
    if fields:
        params["fields"] = fields
    data = await client.get_json("/v2/order", params=params)
    return [Order.model_validate(v) for v in data.get("values", [])]


async def get_order(
    client: TripletexClient,
    order_id: int,
    fields: str = "",
) -> Order:
    """GET /v2/order/{id}"""
    params = {"fields": fields} if fields else {}
    data = await client.get_json(f"/v2/order/{order_id}", params=params)
    return Order.model_validate(data.get("value", data))


async def create_order(
    client: TripletexClient,
    payload: dict[str, Any],
) -> Order:
    """POST /v2/order"""
    data = await client.post_json("/v2/order", json_body=payload)
    return Order.model_validate(data.get("value", data))


async def get_order_line(
    client: TripletexClient,
    order_line_id: int,
    fields: str = "",
) -> OrderLine:
    """GET /v2/order/orderline/{id}"""
    params = {"fields": fields} if fields else {}
    data = await client.get_json(f"/v2/order/orderline/{order_line_id}", params=params)
    return OrderLine.model_validate(data.get("value", data))


async def create_order_line(
    client: TripletexClient,
    payload: dict[str, Any],
) -> OrderLine:
    """POST /v2/order/orderline"""
    data = await client.post_json("/v2/order/orderline", json_body=payload)
    return OrderLine.model_validate(data.get("value", data))
