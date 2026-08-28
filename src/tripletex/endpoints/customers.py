"""Customer endpoints (official API)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tripletex.endpoints._paging import paginate
from tripletex.models import Customer

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def list_customers(
    client: TripletexClient,
    query: str | None = None,
    fields: str = "",
    limit: int | None = None,
) -> list[Customer]:
    """GET /v2/customer. Returns every match unless `limit` is given."""
    params: dict[str, str] = {}
    if query:
        params["query"] = query
    if fields:
        params["fields"] = fields
    values = await paginate(client, "/v2/customer", params=params, limit=limit)
    return [Customer.model_validate(v) for v in values]


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
