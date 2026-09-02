"""Tests for the documents reception queue.

The queue's value is the count and the age, so these pin the parts a monitor
would read — and the permission flag that separates "the queue is empty" from
"my part of it is".
"""

from __future__ import annotations

import datetime
from pathlib import Path

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.documents import (
    download_document,
    get_document_reception_context,
    list_document_reception,
)
from tripletex.models import DocumentReceptionItem
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

# A real row, as measured 2026-09-02.
ROW = {
    "messageId": 174063704,
    "documentId": 1161555237,
    "documentName": "WhatsApp Image 2026-09-01 at 12.06.00.jpeg",
    "receiverEmployeeId": 4229621,
    "receiverName": "Jerome Lacoste",
    "senderName": " ",
    "displaySize": "24,5 KB",
    "size": 25059,
    "created": "2026-09-02",
    "edited": "2026-09-02",
    "isNew": False,
    "mimeType": "image/jpeg",
}

CONTEXT = {
    "value": {
        "documentReceptionEmail": "4229621.inbox@arkiv.tripletex.no",
        "authAllEmployees": True,
        "authVoucherReception": True,
        "maxFileSize": 10485760,
    }
}


def _client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _rows(*rows):
    """Serve `rows` with correct paging, plus Tripletex's misleading count."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pageContext"):
            return httpx.Response(200, json=CONTEXT)
        frm = int(request.url.params.get("from", 0))
        cnt = int(request.url.params.get("count", 1000))
        page = list(rows)[frm : frm + cnt]
        return httpx.Response(
            200,
            json={
                "values": page,
                # Deliberately wrong, the way the real endpoint is.
                "fullResultSize": frm + len(page) + (1 if len(page) == cnt else 0),
            },
        )

    return handler


class TestListing:
    async def test_parses_a_real_row(self):
        (item,) = await list_document_reception(_client(_rows(ROW)))

        assert item.document_id == 1161555237
        assert item.receiver_name == "Jerome Lacoste"
        assert item.created == datetime.date(2026, 9, 2)
        assert item.size == 25059
        assert item.mime_type == "image/jpeg"

    async def test_empty_queue_is_an_empty_list(self):
        assert await list_document_reception(_client(_rows())) == []

    async def test_pages_past_the_first_page(self):
        rows = [dict(ROW, documentId=i) for i in range(1, 2503)]

        items = await list_document_reception(_client(_rows(*rows)))

        assert len(items) == 2502
        assert {i.document_id for i in items} == set(range(1, 2503))

    async def test_misleading_full_result_size_is_not_used_as_a_total(self):
        """It reports from + len + 1 on a full page, which exceeds the truth."""
        rows = [dict(ROW, documentId=i) for i in range(1, 4)]

        items = await list_document_reception(_client(_rows(*rows)))

        assert len(items) == 3

    async def test_limit_stops_early(self):
        rows = [dict(ROW, documentId=i) for i in range(1, 100)]

        assert len(await list_document_reception(_client(_rows(*rows)), limit=5)) == 5


class TestAge:
    def test_age_in_whole_days(self):
        created = datetime.date.today() - datetime.timedelta(days=3)
        item = DocumentReceptionItem.model_validate(
            dict(ROW, created=created.isoformat())
        )

        assert item.age_days == 3

    def test_age_is_none_without_a_date(self):
        item = DocumentReceptionItem.model_validate(dict(ROW, created=None))

        assert item.age_days is None

    def test_fields_measured_to_be_inert_are_still_parsed(self):
        """senderName is always blank and isNew always false, but the payload
        carries them and the model should not choke."""
        item = DocumentReceptionItem.model_validate(ROW)

        assert item.sender_name == " "
        assert item.is_new is False


class TestContext:
    async def test_reads_the_ingest_address_and_rights(self):
        ctx = await get_document_reception_context(_client(_rows()))

        assert ctx.document_reception_email == "4229621.inbox@arkiv.tripletex.no"
        assert ctx.auth_all_employees is True
        assert ctx.auth_voucher_reception is True

    async def test_a_narrowed_view_is_visible_to_the_caller(self):
        """An empty queue means nothing unless this flag is true — the caller
        needs to tell "nothing waiting" from "nothing addressed to me"."""

        def handler(request: httpx.Request) -> httpx.Response:
            narrowed = {"value": dict(CONTEXT["value"], authAllEmployees=False)}
            return httpx.Response(200, json=narrowed)

        ctx = await get_document_reception_context(_client(handler))

        assert ctx.auth_all_employees is False


class TestDownload:
    async def test_writes_the_bytes_and_sends_no_specific_accept(self, tmp_path: Path):
        """Naming a media type is what makes this endpoint 400 — even the
        correct one — so the request must ask for */*."""
        seen: dict[str, str] = {}
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"

        def handler(request: httpx.Request) -> httpx.Response:
            seen["accept"] = request.headers.get("accept", "")
            seen["path"] = request.url.path
            return httpx.Response(200, content=jpeg)

        dest = tmp_path / "out.jpg"
        await download_document(_client(handler), 1161555237, dest)

        assert dest.read_bytes() == jpeg
        assert seen["path"] == "/v2/document/1161555237/content"
        assert seen["accept"] == "*/*"
