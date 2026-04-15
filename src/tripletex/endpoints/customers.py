"""Customer endpoints (official API)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tripletex.models import Customer

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def list_customers(
    client: TripletexClient,
    query: str | None = None,
    fields: str = "",
    count: int = 1000,
) -> list[Customer]:
    """GET /v2/customer"""
    params: dict[str, str] = {"from": "0", "count": str(count)}
    if query:
        params["query"] = query
    if fields:
        params["fields"] = fields
    data = await client.get_json("/v2/customer", params=params)
    return [Customer.model_validate(v) for v in data.get("values", [])]


async def get_customer(
    client: TripletexClient,
    customer_id: int,
    fields: str = "",
) -> Customer:
    """GET /v2/customer/{id}"""
    params = {"fields": fields} if fields else {}
    data = await client.get_json(f"/v2/customer/{customer_id}", params=params)
    return Customer.model_validate(data.get("value", data))


async def create_customer(
    client: TripletexClient,
    payload: dict[str, Any],
) -> Customer:
    """POST /v2/customer"""
    data = await client.post_json("/v2/customer", json_body=payload)
    return Customer.model_validate(data.get("value", data))


async def update_customer(
    client: TripletexClient,
    customer_id: int,
    payload: dict[str, Any],
) -> Customer:
    """PUT /v2/customer/{id}"""
    data = await client.put_json(f"/v2/customer/{customer_id}", json_body=payload)
    return Customer.model_validate(data.get("value", data))
