"""Order endpoints (official API)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from tripletex.endpoints._paging import paginate
from tripletex.models import Order, OrderLine

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


# By default the API collapses nested objects (customer, orderLines, ...) to
# {id, url}. Expand the customer (for its name and invoicing arrangement), the
# order lines (for the per-line amounts we sum into an order total) and the
# preliminary invoice.
#
# `singleCustomerInvoice` is expanded because callers routinely need it to
# decide whether an order is late, and it costs nothing alongside the name.
# What it implies about timing is the caller's policy — this library reports
# the field and derives nothing from it.
_ORDER_FIELDS = (
    "*,"
    "customer(id,name,organizationNumber,singleCustomerInvoice),"
    "orderLines(amountExcludingVatCurrency,amountIncludingVatCurrency),"
    "preliminaryInvoice(id,invoiceNumber,invoiceDate,isApproved,voucher(id))"
)


async def list_orders(
    client: TripletexClient,
    order_date_from: date,
    order_date_to: date,
    fields: str = _ORDER_FIELDS,
    limit: int | None = None,
) -> list[Order]:
    """GET /v2/order. Date range is half-open [from, to) — to is exclusive.

    Returns every order in the range unless `limit` is given.
    """
    params: dict[str, str] = {
        "orderDateFrom": order_date_from.isoformat(),
        "orderDateTo": order_date_to.isoformat(),
    }
    if fields:
        params["fields"] = fields
    values = await paginate(client, "/v2/order", params=params, limit=limit)
    return [Order.model_validate(v) for v in values]


async def get_order(
    client: TripletexClient,
    order_id: int,
    fields: str = _ORDER_FIELDS,
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


async def list_open_orders(
    client: TripletexClient,
    order_date_from: date | None = None,
    order_date_to: date | None = None,
    fields: str = _ORDER_FIELDS,
) -> list[Order]:
    """Orders not yet invoiced, filtered server-side.

    GET /v2/order?isClosed=false. The filter is applied by Tripletex, so this
    returns the queue rather than the whole order book: 12 and 7 rows against
    844 and 646 orders on two measured companies. One request, cheap enough to
    poll.

    `is_closed` is the reliable signal that invoicing happened. `preliminary
    Invoice` is *cleared* once an order is invoiced rather than gaining a
    voucher, so an open order's draft invoice always reads
    `invoiceNumber: 0, voucher: null` — true of all 19 measured, which makes it
    useless as a discriminator on its own.

    When an open order becomes *late* is not answered here: Tripletex states no
    invoicing deadline, so that is the caller's policy to apply over these rows.
    """
    params: dict[str, str] = {"isClosed": "false", "fields": fields}
    # The endpoint wants a date window; default to one wide enough for anything
    # still open, since a genuinely stuck order can be arbitrarily old.
    params["orderDateFrom"] = (order_date_from or date(2000, 1, 1)).isoformat()
    params["orderDateTo"] = (order_date_to or date.today()).isoformat()

    values = await paginate(client, "/v2/order", params=params)
    return [Order.model_validate(v) for v in values]
