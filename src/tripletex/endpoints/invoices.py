"""Invoice endpoints (official API)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from tripletex.endpoints._paging import paginate
from tripletex.models import Invoice, Reminder

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


# By default the API collapses nested objects (customer, currency, orders) to
# {id, url}. Expand them so list output can show name, currency and reference.
_INVOICE_FIELDS = (
    "*,"
    "customer(id,name,organizationNumber),"
    "currency(id,code),"
    "orders(id,number,reference)"
)


async def list_invoices(
    client: TripletexClient,
    invoice_date_from: date,
    invoice_date_to: date,
    fields: str = _INVOICE_FIELDS,
    limit: int | None = None,
) -> list[Invoice]:
    """GET /v2/invoice. Date range is half-open [from, to) — to is exclusive.

    Returns every invoice in the range unless `limit` is given.
    """
    params: dict[str, str] = {
        "invoiceDateFrom": invoice_date_from.isoformat(),
        "invoiceDateTo": invoice_date_to.isoformat(),
    }
    if fields:
        params["fields"] = fields
    values = await paginate(client, "/v2/invoice", params=params, limit=limit)
    return [Invoice.model_validate(v) for v in values]


async def get_invoice(
    client: TripletexClient,
    invoice_id: int,
    fields: str = _INVOICE_FIELDS,
) -> Invoice:
    """GET /v2/invoice/{id}"""
    params = {"fields": fields} if fields else {}
    data = await client.get_json(f"/v2/invoice/{invoice_id}", params=params)
    return Invoice.model_validate(data.get("value", data))


async def list_invoices_for_order(
    client: TripletexClient,
    order_id: int,
    fields: str = "id,invoiceNumber,invoiceDate",
) -> list[Invoice]:
    """Invoices belonging to an order.

    GET /v2/invoice/{orderId}/invoices — the only way to map an order to its
    invoice(s); orders carry no back-reference and /v2/invoice has no order filter.
    """
    params = {"fields": fields} if fields else {}
    data = await client.get_json(f"/v2/invoice/{order_id}/invoices", params=params)
    return [Invoice.model_validate(v) for v in data.get("values", [])]


async def create_invoice(
    client: TripletexClient,
    payload: dict[str, Any],
) -> Invoice:
    """POST /v2/invoice"""
    data = await client.post_json("/v2/invoice", json_body=payload)
    return Invoice.model_validate(data.get("value", data))


async def list_reminders(
    client: TripletexClient,
    date_from: date,
    date_to: date,
    customer_id: int | None = None,
    limit: int | None = None,
) -> list[Reminder]:
    """Reminders sent on unpaid invoices over a date range.

    GET /v2/reminder — documented, and reachable with API-token auth.

    Two things to get right, both of which cost a wrong turn:

    - **The path is `/v2/reminder`, not `/v2/invoice/reminder`**, which 422s and
      looks like the feature is web-only.
    - **The date parameters are `dateFrom`/`dateTo`**, not the `invoiceDateFrom`
      pair the invoice endpoints take; the wrong names also 422.

    Prefer this over the `reminders` array nested in `/v2/invoice`. That works
    but needs a full invoice sweep to find a handful of rows, cannot be filtered
    by date server-side, and buries `charge`, `interests` and `interestRate`.

    `fields` is deliberately not used: the endpoint rejects a nested
    `invoice(...)` expansion with 400, so callers join on `invoice_id`.
    """
    params = {"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()}
    if customer_id is not None:
        params["customerId"] = str(customer_id)
    values = await paginate(client, "/v2/reminder", params=params, limit=limit)
    return [Reminder.model_validate(r) for r in values]
