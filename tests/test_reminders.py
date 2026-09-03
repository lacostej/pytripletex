"""The two unrelated things Tripletex calls a reminder.

`endpoints.invoices.list_reminders` chases a customer for money and is a
documented, token-reachable endpoint. `endpoints.dashboard` surfaces statutory
deadlines and is web-session only. The shared word is Tripletex's, not ours, and
conflating them is easy — the payloads below are real, captured 2026-09-03.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.dashboard import list_dashboard_reminders
from tripletex.endpoints.invoices import list_reminders
from tripletex.session import ApiSession, WebSession, WebSessionRequired

BASE_URL = "https://tripletex.no"

# Bonita Services, a formal purring carrying a fee and interest.
REMINDER = {
    "id": 10026086,
    "invoiceId": 1857768223,
    "reminderDate": "2025-05-07",
    "charge": 70.0,
    "totalCharge": 70.0,
    "interests": 670.61,
    "interestRate": 0.0,
    "totalAmountCurrency": 1050.0,
    "termOfPayment": "2025-05-21",
    "type": "REMINDER",
    "comment": "",
}

SOFT = {**REMINDER, "id": 10026123, "type": "SOFT_REMINDER",
        "charge": 0.0, "totalCharge": 0.0, "interests": 0.0}

# The dashboard payload, three levels deep, with a null companyReminder on the
# rows the company has not started.
DASHBOARD = {
    "value": [
        {
            "globalReminder": {
                "id": 102, "name": "TAX_NOTICE",
                "displayName": "Skattemelding (2025)",
                "deadline": "2026-05-31", "term": "YEARLY", "remainingDays": -3,
                "reminderUrl": "https://tripletex.no/execute/yearEnd/submission",
            },
            "companyReminder": {
                "id": 2422838, "submissionDate": "2026-09-03",
                "status": "NOT_COMPLETED", "globalReminderId": 102,
            },
            "reminderBorderColor": "RED",
        },
        {
            "globalReminder": {
                "id": 97, "name": "A_MELDING", "displayName": "A-melding (aug)",
                "deadline": "2026-09-07", "term": "AUG", "remainingDays": 4,
                "reminderUrl": "https://tripletex.no/execute/ameldingWageMenu",
            },
            "companyReminder": None,
            "reminderBorderColor": "YELLOW",
        },
        {
            "globalReminder": {
                "id": 106, "name": "TAX_PAYMENT",
                "displayName": "Betaling av AGA (juli-aug)",
                "deadline": "2026-09-15", "term": "JUL_AUG", "remainingDays": 12,
                "reminderUrl": "https://tripletex.no/execute/wagePeriodTransactionMenu",
            },
            "companyReminder": None,
            "reminderBorderColor": "NONE",
        },
    ]
}


def _api_client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _web_client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = WebSession(cookies=httpx.Cookies(), context_id="1")
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _rows(*rows):
    def handler(request: httpx.Request) -> httpx.Response:
        frm = int(request.url.params.get("from", 0))
        cnt = int(request.url.params.get("count", 1000))
        page = list(rows)[frm : frm + cnt]
        return httpx.Response(200, json={"values": page, "fullResultSize": len(page)})

    return handler


YEAR = (datetime.date(2025, 1, 1), datetime.date(2025, 12, 31))


class TestInvoiceReminders:
    async def test_parses_a_formal_reminder(self):
        (r,) = await list_reminders(_api_client(_rows(REMINDER)), *YEAR)

        assert r.invoice_id == 1857768223
        assert r.reminder_date == datetime.date(2025, 5, 7)
        assert r.is_formal
        assert r.total_charge == Decimal("70")
        assert r.interests == Decimal("670.61")

    async def test_soft_reminder_is_not_formal(self):
        """32 of 39 measured reminders were soft. A soft reminder is a courtesy
        nudge and is not supposed to carry a fee, so counting the two together
        makes a company look as though it never charges."""
        (r,) = await list_reminders(_api_client(_rows(SOFT)), *YEAR)

        assert not r.is_formal
        assert r.cost_to_customer == Decimal(0)

    async def test_cost_sums_charge_and_interest(self):
        (r,) = await list_reminders(_api_client(_rows(REMINDER)), *YEAR)

        assert r.cost_to_customer == Decimal("740.61")

    async def test_uses_dateFrom_not_invoiceDateFrom(self):
        """`invoiceDateFrom` 422s, which reads like the endpoint is unavailable."""
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_reminders(_api_client(handler), *YEAR)

        assert seen[0].params["dateFrom"] == "2025-01-01"
        assert seen[0].params["dateTo"] == "2025-12-31"
        assert "invoiceDateFrom" not in seen[0].params

    async def test_hits_the_reminder_path_not_invoice_reminder(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_reminders(_api_client(handler), *YEAR)

        assert seen[0] == "/v2/reminder"

    async def test_customer_filter_is_passed_through(self):
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_reminders(_api_client(handler), *YEAR, customer_id=42)

        assert seen[0].params["customerId"] == "42"


class TestDashboardReminders:
    def _handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DASHBOARD)

    async def test_flattens_the_three_levels(self):
        first, *_ = await list_dashboard_reminders(_web_client(self._handler))

        assert first.name == "TAX_NOTICE"
        assert first.display_name == "Skattemelding (2025)"
        assert first.deadline == datetime.date(2026, 5, 31)
        assert first.status == "NOT_COMPLETED"
        assert first.border_color == "RED"

    async def test_missing_company_row_leaves_status_unset(self):
        """No companyReminder means the company has not started. That is an
        absence, not a status to invent."""
        reminders = await list_dashboard_reminders(_web_client(self._handler))
        a_melding = next(r for r in reminders if r.name == "A_MELDING")

        assert a_melding.status is None
        assert not a_melding.is_done

    async def test_urgency_follows_tripletex_own_colour(self):
        reminders = await list_dashboard_reminders(_web_client(self._handler))

        assert [r.name for r in reminders if r.is_urgent] == ["TAX_NOTICE", "A_MELDING"]

    async def test_urgent_only_filters(self):
        reminders = await list_dashboard_reminders(
            _web_client(self._handler), urgent_only=True
        )

        assert len(reminders) == 2
        assert all(r.is_urgent for r in reminders)

    async def test_overdue_is_negative_remaining_days(self):
        reminders = await list_dashboard_reminders(_web_client(self._handler))

        assert reminders[0].name == "TAX_NOTICE"
        assert reminders[0].is_overdue
        assert not reminders[-1].is_overdue

    async def test_sorted_by_urgency_of_deadline(self):
        reminders = await list_dashboard_reminders(_web_client(self._handler))

        assert [r.remaining_days for r in reminders] == [-3, 4, 12]

    async def test_reads_value_not_values(self):
        """The dashboard endpoint keys its list `value`, unlike every /v2 list."""

        def wrong_key(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"values": DASHBOARD["value"]})

        assert await list_dashboard_reminders(_web_client(wrong_key)) == []

    async def test_api_token_auth_is_refused_up_front(self):
        """The endpoint answers 403 to a token, so fail with an instruction
        rather than an HTTP error."""
        with pytest.raises(WebSessionRequired):
            await list_dashboard_reminders(_api_client(self._handler))
