"""Tests for the travel expense approval queue.

The queue's value is "who is waiting, how long, for how much", so these pin the
count, the age basis and the state filter.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.expenses import (
    AWAITING_APPROVAL,
    list_travel_expenses,
    validate_state,
)
from tripletex.models import TravelExpense
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

# A real row, as measured 2026-09-02 on Bonita Handel.
ROW = {
    "id": 12254790,
    "url": "tripletex.no/v2/travelExpense/12254790",
    "amount": 935.38,
    "paymentAmount": 935.38,
    "project": None,
    "employee": {
        "id": 4888744,
        "firstName": "Beatriz Eugenia",
        "lastName": "Bustillo Martinez",
    },
    "approvedBy": None,
    "department": {"id": 263009, "name": "Avdeling"},
    "voucher": None,
    "isCompleted": True,
    "isApproved": False,
    "rejectedComment": "",
    "completedDate": "2026-08-15",
    "approvedDate": None,
    "date": "2026-08-15",
    "number": 27,
    "title": "",
    "attachmentCount": 1,
    "state": "DELIVERED",
    "stateName": "Levert",
}


def _client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _rows(*rows, capture: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.update(dict(request.url.params))
        frm = int(request.url.params.get("from", 0))
        cnt = int(request.url.params.get("count", 1000))
        page = list(rows)[frm : frm + cnt]
        return httpx.Response(200, json={"values": page, "fullResultSize": len(rows)})

    return handler


class TestListing:
    async def test_parses_a_real_row(self):
        (claim,) = await list_travel_expenses(_client(_rows(ROW)))

        assert claim.id == 12254790
        assert claim.employee_name == "Beatriz Eugenia Bustillo Martinez"
        assert claim.amount == Decimal("935.38")
        assert claim.state == "DELIVERED"
        assert claim.is_approved is False
        assert claim.attachment_count == 1

    async def test_defaults_to_the_approval_queue(self):
        seen: dict = {}
        await list_travel_expenses(_client(_rows(ROW, capture=seen)))

        assert seen["state"] == AWAITING_APPROVAL == "DELIVERED"

    async def test_state_is_passed_through(self):
        seen: dict = {}
        await list_travel_expenses(_client(_rows(capture=seen)), state="APPROVED")

        assert seen["state"] == "APPROVED"

    async def test_employee_filter_is_passed_through(self):
        seen: dict = {}
        await list_travel_expenses(_client(_rows(capture=seen)), employee_id=4888744)

        assert seen["employeeId"] == "4888744"

    async def test_pages_past_the_first_page(self):
        rows = [dict(ROW, id=i) for i in range(1, 2503)]

        claims = await list_travel_expenses(_client(_rows(*rows)))

        assert len(claims) == 2502

    async def test_empty_queue_is_an_empty_list(self):
        assert await list_travel_expenses(_client(_rows())) == []


class TestState:
    @pytest.mark.parametrize(
        "state", ["ALL", "OPEN", "DELIVERED", "APPROVED", "REJECTED", "SALARY_PAID"]
    )
    def test_documented_states_are_accepted(self, state):
        assert validate_state(state) == state

    def test_an_unknown_state_is_refused_before_a_request(self):
        # Otherwise it costs a round-trip to learn the endpoint 400s.
        with pytest.raises(ValueError, match="Unknown travel expense state"):
            validate_state("PENDING")

    async def test_a_bad_state_never_reaches_the_wire(self):
        def explode(request):  # pragma: no cover - must not be reached
            raise AssertionError("sent a request with an invalid state")

        with pytest.raises(ValueError):
            await list_travel_expenses(_client(explode), state="NOPE")


class TestWaitingTime:
    """Aging must count from submission, not from when the expense happened."""

    def test_ages_from_submission_not_the_expense_date(self):
        # Measured live: claims dated 2026-08-04 and 08-08 were submitted on
        # 09-01. Aging from `date` would report a month's wait for something
        # filed yesterday.
        today = datetime.date.today()
        claim = TravelExpense.model_validate(
            dict(
                ROW,
                date=(today - datetime.timedelta(days=29)).isoformat(),
                completedDate=(today - datetime.timedelta(days=1)).isoformat(),
            )
        )

        assert claim.waiting_days == 1

    def test_falls_back_to_the_expense_date(self):
        today = datetime.date.today()
        claim = TravelExpense.model_validate(
            dict(ROW, completedDate=None, date=(today - datetime.timedelta(days=4)).isoformat())
        )

        assert claim.waiting_days == 4

    def test_is_none_when_undated(self):
        claim = TravelExpense.model_validate(dict(ROW, completedDate=None, date=None))

        assert claim.waiting_days is None


class TestEmployeeName:
    def test_joins_the_parts(self):
        assert TravelExpense.model_validate(ROW).employee_name == (
            "Beatriz Eugenia Bustillo Martinez"
        )

    def test_survives_a_missing_employee(self):
        assert TravelExpense.model_validate(dict(ROW, employee=None)).employee_name == ""
