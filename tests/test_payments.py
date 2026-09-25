"""Listing payments across bank integrations.

`paymentSource` filters server side: a company on Ztl answers an AutoPay filter
with 200 and no rows, and an unknown value with 404. Omitted, every source comes
back. Modelled on the live responses, 2026-09-25.
"""

from __future__ import annotations

import httpx

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.payments import list_payments
from tripletex.session import WebSession

BASE_URL = "https://tripletex.no"
SOURCES = ("AutoPayTransaction", "ZtlTransaction", "FolioTransaction")


def _client(rows) -> TripletexClient:
    def handler(request: httpx.Request) -> httpx.Response:
        source = request.url.params.get("paymentSource", "")
        if source and source not in SOURCES:
            return httpx.Response(404, json={"status": 404})
        values = [r for r in rows if not source or r["paymentSource"] == source]
        frm = int(request.url.params.get("from", 0))
        cnt = int(request.url.params.get("count", 1000))
        page = values[frm : frm + cnt]
        return httpx.Response(200, json={"values": page, "fullResultSize": len(page)})

    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = WebSession(cookies=httpx.Cookies(), context_id="1")
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


class TestListPayments:
    async def test_ztl_company_payments_are_listed(self):
        client = _client([{"id": 1, "status": "FOR_APPROVAL", "paymentSource": "ZtlTransaction"}])

        payments = await list_payments(client)

        assert [(p.id, p.payment_source) for p in payments] == [(1, "ZtlTransaction")]

    async def test_every_source_is_listed(self):
        client = _client([
            {"id": 1, "status": "FOR_APPROVAL", "paymentSource": "AutoPayTransaction"},
            {"id": 2, "status": "FOR_APPROVAL", "paymentSource": "ZtlTransaction"},
        ])

        payments = await list_payments(client)

        assert sorted(p.id for p in payments) == [1, 2]
