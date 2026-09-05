"""Reading the order queue — the data, not a deadline.

`isClosed=false` is Tripletex's own filter and `singleCustomerInvoice` is its
own field. When an open order becomes *late* is a caller's policy: Tripletex
states no invoicing deadline, so nothing here derives one.

Rows modelled on the live queue, 2026-09-05: Handel 12 open, Services 7.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.orders import list_open_orders
from tripletex.models import Order
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

PRELIM = {"id": 2175433408, "invoiceNumber": 0, "invoiceDate": "2026-07-09",
          "isApproved": True, "voucher": None}


def _order(order_id, order_date, customer, single_invoice, amount=100.0):
    customer_obj = {"id": order_id, "name": customer}
    if single_invoice is not None:
        customer_obj["singleCustomerInvoice"] = single_invoice
    return {
        "id": order_id, "orderDate": order_date, "isClosed": False,
        "customer": customer_obj, "preliminaryInvoice": PRELIM,
        "orderLines": [{"amountIncludingVatCurrency": amount}],
    }


def _client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _rows(*orders):
    def handler(request: httpx.Request) -> httpx.Response:
        frm = int(request.url.params.get("from", 0))
        cnt = int(request.url.params.get("count", 1000))
        page = list(orders)[frm : frm + cnt]
        return httpx.Response(200, json={"values": page, "fullResultSize": len(page)})

    return handler


class TestOpenOrders:
    async def test_filters_server_side(self):
        """12 rows out of 844, not 844 filtered locally."""
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_open_orders(_client(handler))

        assert seen[0].params["isClosed"] == "false"

    async def test_asks_for_the_customer_invoicing_flag(self):
        """Callers need it to judge lateness; fetching it costs nothing here."""
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_open_orders(_client(handler))

        assert "singleCustomerInvoice" in seen[0].params["fields"]

    async def test_date_window_defaults_wide(self):
        """A stuck order can be arbitrarily old, so the window must not hide it."""
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_open_orders(_client(handler))

        assert seen[0].params["orderDateFrom"] == "2000-01-01"

    async def test_parses_the_queue(self):
        orders = await list_open_orders(
            _client(_rows(_order(1, "2026-08-05", "Strak Strak AS", True, 1156.0)))
        )

        assert orders[0].customer_name == "Strak Strak AS"
        assert orders[0].amount_including_vat == Decimal("1156")
        assert orders[0].preliminary_invoice["invoiceNumber"] == 0


class TestSingleInvoiceFlag:
    """Reported, never interpreted. What it implies about timing is policy."""

    def test_reports_the_field(self):
        on = Order.model_validate(_order(1, "2026-08-05", "A", True))
        off = Order.model_validate(_order(2, "2026-06-18", "B", False))

        assert on.customer_single_invoice is True
        assert off.customer_single_invoice is False

    def test_unexpanded_customer_is_none_not_false(self):
        """`None` means "not asked for", which a caller must be able to tell
        from the flag being off — they imply opposite deadlines."""
        o = Order.model_validate(_order(3, "2026-08-05", "C", None))

        assert o.customer_single_invoice is None

    def test_no_customer_at_all_is_none(self):
        o = Order.model_validate({"id": 4, "orderDate": "2026-08-05"})

        assert o.customer_single_invoice is None
