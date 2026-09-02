"""Tests for chart-of-accounts and posting access.

These pin the shapes the accounting checks depend on: that a posting carries its
own VAT treatment separately from the account's default, that a missing receipt
is `None` rather than absent, and that a voucher with no type parses instead of
raising — 72 of 326 vouchers in one measured month had none.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.ledger import (
    list_accounts,
    list_postings,
    list_vat_types,
    list_voucher_types,
    list_vouchers_with_postings,
)
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

# Bonita Handel, voucher 869, July 2026 — a supplier invoice with a receipt.
VOUCHER_WITH_RECEIPT = {
    "id": 637636527,
    "number": 869,
    "tempNumber": 16500,
    "year": 2026,
    "date": "2026-07-01",
    "description": "Faktura nummer 10211 fra KAMMER EIENDOM AS",
    "voucherType": {"id": 3015037, "name": "Leverandørfaktura"},
    "attachment": {
        "id": 1085979613,
        "fileName": "invoice-10211.pdf",
        "size": 54028,
        "mimeType": "application/pdf",
    },
    "postings": [
        {
            "id": 4006687016,
            "date": "2026-07-01",
            "description": "Faktura nummer 10211 fra KAMMER EIENDOM AS",
            "account": {"number": 6300, "name": "Leie lokale", "type": "OPERATING_EXPENSES"},
            "supplier": {"id": 36953507, "name": "KAMMER EIENDOM AS"},
            "customer": None,
            "employee": None,
            "vatType": {"id": 1, "name": "Fradrag inngående avgift, høy sats", "percentage": 25},
            "amount": 29392.0,
            "amountCurrency": 29392.0,
            "currency": {"code": "NOK"},
            "row": 1,
        },
        {
            "id": 4006687017,
            "date": "2026-07-01",
            "account": {"number": 2400, "name": "Leverandørgjeld", "type": "LIABILITIES"},
            "supplier": {"id": 36953507, "name": "KAMMER EIENDOM AS"},
            "vatType": {"id": 0, "name": "Ingen avgiftsbehandling", "percentage": 0},
            "amount": -36740.0,
            "amountCurrency": -36740.0,
            "currency": {"code": "NOK"},
            "row": 1,
        },
    ],
}

# Voucher 1123 — a payment, correctly carrying no document and no type.
VOUCHER_NO_RECEIPT = {
    "id": 654807929,
    "number": 1123,
    "date": "2026-07-03",
    "description": "Direkte betaling med ekstern ID TR590042975 er gjennomført og bokført.",
    "voucherType": None,
    "attachment": None,
    "postings": [],
}

ACCOUNT = {
    "id": 102962747,
    "number": 1000,
    "name": "Forskning og utvikling, ervervet",
    "type": "ASSETS",
    "vatType": {"id": 0, "name": "Ingen avgiftsbehandling", "percentage": 0},
    "isInactive": False,
    "isBankAccount": False,
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


JULY = (datetime.date(2026, 7, 1), datetime.date(2026, 7, 31))


class TestVouchersWithPostings:
    async def test_parses_receipt_and_postings(self):
        (v,) = await list_vouchers_with_postings(
            _client(_rows(VOUCHER_WITH_RECEIPT)), *JULY
        )

        assert v.number == 869
        assert v.has_attachment
        assert v.attachment["fileName"] == "invoice-10211.pdf"
        assert v.voucher_type_name == "Leverandørfaktura"
        assert v.date == datetime.date(2026, 7, 1)
        assert len(v.postings) == 2

    async def test_posting_carries_its_own_vat_treatment(self):
        """The expense line deducts 25%; the payable line is outside VAT. A check
        that read the treatment off the voucher would conflate the two."""
        (v,) = await list_vouchers_with_postings(
            _client(_rows(VOUCHER_WITH_RECEIPT)), *JULY
        )
        expense, payable = v.postings

        assert expense.account.number == 6300
        assert expense.vat_type.id == 1
        assert expense.vat_type.percentage == Decimal("25")
        assert expense.amount == Decimal("29392")
        assert expense.supplier["name"] == "KAMMER EIENDOM AS"

        assert payable.account.number == 2400
        assert payable.vat_type.id == 0
        assert payable.amount == Decimal("-36740")

    async def test_missing_receipt_and_type_parse_as_none(self):
        """Payment runs legitimately have neither. Both must be absences, not
        errors — 127 of 127 Remittering vouchers in one July had no attachment."""
        (v,) = await list_vouchers_with_postings(
            _client(_rows(VOUCHER_NO_RECEIPT)), *JULY
        )

        assert v.attachment is None
        assert not v.has_attachment
        assert v.voucher_type_name is None
        assert v.postings == []

    async def test_changes_are_requested_only_when_asked(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("fields", ""))
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_vouchers_with_postings(_client(handler), *JULY)
        assert "changes" not in seen[0]

        await list_vouchers_with_postings(_client(handler), *JULY, include_changes=True)
        assert seen[1].endswith(",changes")

    async def test_date_range_is_sent(self):
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_vouchers_with_postings(_client(handler), *JULY)

        assert seen[0].params["dateFrom"] == "2026-07-01"
        assert seen[0].params["dateTo"] == "2026-07-31"


class TestAccounts:
    async def test_parses_account_with_default_vat(self):
        (a,) = await list_accounts(_client(_rows(ACCOUNT)))

        assert a.number == 1000
        assert a.type == "ASSETS"
        assert a.vat_type.id == 0
        assert not a.is_bank_account
        assert not a.is_inactive

    async def test_include_inactive_controls_the_filter(self):
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_accounts(_client(handler))
        assert "isInactive" not in seen[0].params

        await list_accounts(_client(handler), include_inactive=False)
        assert seen[1].params["isInactive"] == "false"

    async def test_pages_past_the_first_thousand(self):
        """603 accounts fit in one page, but a bigger company's would not, and
        `fullResultSize` cannot be trusted to say so."""
        rows = [{**ACCOUNT, "id": i, "number": i} for i in range(1, 1502)]
        got = await list_accounts(_client(_rows(*rows)))

        assert len(got) == 1501


class TestVatAndVoucherTypes:
    async def test_negative_percentage_parses(self):
        """Type 34 is -25%, used to reverse input VAT on a credit note."""
        rows = [
            {"id": 1, "name": "Fradrag inngående avgift, høy sats", "percentage": 25},
            {"id": 34, "name": "Fradrag inngående avgift, høy sats, kreditnota", "percentage": -25},
        ]
        high, credit = await list_vat_types(_client(_rows(*rows)))

        assert high.percentage == Decimal("25")
        assert credit.percentage == Decimal("-25")

    async def test_voucher_types_come_back_keyed_by_id(self):
        rows = [
            {"id": 3015037, "name": "Leverandørfaktura"},
            {"id": 3015044, "name": "Remittering"},
        ]
        types = await list_voucher_types(_client(_rows(*rows)))

        assert types == {3015037: "Leverandørfaktura", 3015044: "Remittering"}


class TestPostings:
    POSTING = {
        "id": 2084130993,
        "voucher": {"id": 341314909, "number": 2674},
        "date": "2026-07-01",
        "description": "Oppvaskmaskin. Periodisering",
        "account": {"number": 6015, "name": "Avskrivning på maskiner og inventar"},
        "supplier": {"name": "Turnor Store AS"},
        "vatType": {"id": 0, "name": "Ingen avgiftsbehandling", "percentage": 0},
        "amount": 1103.89,
    }

    async def test_parses_a_posting(self):
        (p,) = await list_postings(_client(_rows(self.POSTING)), *JULY)

        assert p.account.number == 6015
        assert p.amount == Decimal("1103.89")
        assert p.date == datetime.date(2026, 7, 1)

    async def test_account_numbers_filter_client_side(self):
        other = {**self.POSTING, "id": 2, "account": {"number": 7770, "name": "Bank og kortgebyrer"}}
        client = _client(_rows(self.POSTING, other))

        fees = await list_postings(client, *JULY, account_numbers=[7770])

        assert [p.account.number for p in fees] == [7770]

    async def test_posting_without_an_account_survives_the_filter(self):
        """`account` is nullable in the schema; filtering must not raise on it."""
        headless = {**self.POSTING, "id": 3, "account": None}
        client = _client(_rows(self.POSTING, headless))

        got = await list_postings(client, *JULY, account_numbers=[6015])

        assert [p.id for p in got] == [2084130993]
