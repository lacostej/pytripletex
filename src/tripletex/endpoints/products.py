"""Product endpoints (official API)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tripletex.models import Product

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def list_products(
    client: TripletexClient,
    query: str | None = None,
    fields: str = "",
    count: int = 1000,
) -> list[Product]:
    """GET /v2/product"""
    params: dict[str, str] = {"from": "0", "count": str(count)}
    if query:
        params["query"] = query
    if fields:
        params["fields"] = fields
    data = await client.get_json("/v2/product", params=params)
    return [Product.model_validate(v) for v in data.get("values", [])]


async def get_product(
    client: TripletexClient,
    product_id: int,
    fields: str = "",
) -> Product:
    """GET /v2/product/{id}"""
    params = {"fields": fields} if fields else {}
    data = await client.get_json(f"/v2/product/{product_id}", params=params)
    return Product.model_validate(data.get("value", data))


async def create_product(
    client: TripletexClient,
    payload: dict[str, Any],
) -> Product:
    """POST /v2/product"""
    data = await client.post_json("/v2/product", json_body=payload)
    return Product.model_validate(data.get("value", data))
