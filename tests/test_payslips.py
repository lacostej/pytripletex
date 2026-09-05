"""Tests for reading payslips — what was actually paid.

Two things carry the weight: the half-open period filter, whose conversion from
an inclusive span is the easiest thing in the module to get wrong, and the
refusal to return a silently empty result.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.payslips import (
    EmptyPayslipResult,
    list_payslips,
    payslips_or_raise,
)
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

# Shaped after a real Bonita Handel payslip captured 2026-09-05: the field
# names, nesting and types are verbatim, the identifiers and figures are not.
# Names, ids and amounts are invented — a fixture does not need a real person's
# salary to pin a response shape.
#
# The shapes a caller would guess wrong, which is what this fixture exists for:
# `transaction` not `salaryTransaction`, `amount` for net with no `netAmount`,
# `department` on the payslip rather than a specification, and `employeeNumber`
# as a string.
ROW = {
    "id": 1001,
    "year": 2026,
    "month": 8,
    "date": "2026-09-04",
    "amount": 26000.00,
    "grossAmount": 30000.00,
    "vacationAllowanceAmount": 0.0,
    "employee": {
        "id": 501,
        "firstName": "Kari",
        "lastName": "Nordmann",
        "employeeNumber": "701",
    },
    "transaction": {"id": 9001},
    "department": {"id": 11, "name": "Avdeling"},
    "specifications": [
        {
            "id": 2001,
            "salaryType": {"id": 10, "number": "2001", "name": "Timelønn"},
            "rate": 200.0, "count": 140.00, "amount": 28000.00,
            "description": "Cafe",
        },
        {
            "id": 2002,
            "salaryType": {"id": 20, "number": "2006", "name": "Overtid 40 %"},
            "rate": 80.0, "count": 5.00, "amount": 400.00,
            "description": "Overtid 40% opptjent 2026-08",
        },
        {
            "id": 2003,
            "salaryType": {"id": 30, "number": "2030", "name": "Tips (drikkepenger)"},
            "rate": 1600.00, "count": 1.0, "amount": 1600.00,
            "description": "Cafe",
        },
    ],
}


def _client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _rows(*rows, capture: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(dict(request.url.params))
        frm = int(request.url.params.get("from", 0))
        cnt = int(request.url.params.get("count", 1000))
        page = list(rows)[frm : frm + cnt]
        return httpx.Response(200, json={"values": page, "fullResultSize": len(rows)})

    return handler


ONE_MONTH = {"start": (2026, 8), "end": (2026, 8)}


class TestParsing:
    async def test_reads_a_captured_payslip(self):
        (slip,) = await list_payslips(_client(_rows(ROW)), **ONE_MONTH)

        assert slip.period == "2026-08"
        assert slip.employee_number == 701
        assert slip.gross_amount == Decimal("30000.00")
        assert slip.amount == Decimal("26000.00")
        assert len(slip.specifications) == 3

    async def test_payment_date_is_not_the_period(self):
        """A 2026-08 payslip is dated 2026-09-04 — the month it was paid."""
        (slip,) = await list_payslips(_client(_rows(ROW)), **ONE_MONTH)

        assert slip.date == datetime.date(2026, 9, 4)
        assert (slip.year, slip.month) == (2026, 8)

    async def test_wage_type_is_read_from_the_nested_salary_type(self):
        (slip,) = await list_payslips(_client(_rows(ROW)), **ONE_MONTH)

        assert [s.wage_type for s in slip.specifications] == [2001, 2006, 2030]

    async def test_totals_by_wage_type(self):
        """Reconciliation keys on wage type, so summing by it is the core need."""
        (slip,) = await list_payslips(_client(_rows(ROW)), **ONE_MONTH)

        assert slip.total_for(2001) == Decimal("28000.00")
        assert slip.total_for(2006) == Decimal("400.00")
        assert slip.total_for(2004) == Decimal(0)

    async def test_employee_number_survives_being_a_string(self):
        (slip,) = await list_payslips(_client(_rows(ROW)), **ONE_MONTH)
        assert slip.employee_number == 701

    async def test_missing_employee_number_does_not_raise(self):
        row = {**ROW, "employee": {"id": 1}}
        (slip,) = await list_payslips(_client(_rows(row)), **ONE_MONTH)

        assert slip.employee_number is None


class TestHalfOpenPeriodFilter:
    async def test_one_month_asks_for_the_next_month_as_the_bound(self):
        """`monthTo` is exclusive: August alone is 8 -> 9."""
        seen: list = []
        await list_payslips(_client(_rows(ROW, capture=seen)), **ONE_MONTH)

        assert seen[0]["yearFrom"] == "2026"
        assert seen[0]["monthFrom"] == "8"
        assert seen[0]["yearTo"] == "2026"
        assert seen[0]["monthTo"] == "9"

    async def test_december_rolls_the_year_rather_than_asking_for_month_13(self):
        """The bound after 2025-12 is 2026-01, not monthTo=13, which 422s."""
        seen: list = []
        await list_payslips(
            _client(_rows(ROW, capture=seen)), start=(2025, 12), end=(2025, 12)
        )

        assert (seen[0]["yearTo"], seen[0]["monthTo"]) == ("2026", "1")

    async def test_a_full_year_is_expressible(self):
        """api-gaps.md §10 read this as impossible; it follows from half-open."""
        seen: list = []
        await list_payslips(
            _client(_rows(ROW, capture=seen)), start=(2026, 1), end=(2026, 12)
        )

        assert (seen[0]["yearFrom"], seen[0]["monthFrom"]) == ("2026", "1")
        assert (seen[0]["yearTo"], seen[0]["monthTo"]) == ("2027", "1")

    async def test_a_span_crossing_years_is_one_call(self):
        """Measured: 2025-06..2026-08 returned 411 rows in a single request."""
        seen: list = []
        await list_payslips(
            _client(_rows(ROW, capture=seen)), start=(2025, 6), end=(2026, 8)
        )

        assert len(seen) == 1
        assert (seen[0]["yearFrom"], seen[0]["monthFrom"]) == ("2025", "6")
        assert (seen[0]["yearTo"], seen[0]["monthTo"]) == ("2026", "9")

    @pytest.mark.parametrize("month", [0, 13, -1])
    async def test_rejects_a_month_outside_1_12(self, month):
        with pytest.raises(ValueError, match="1-12"):
            await list_payslips(
                _client(_rows(ROW)), start=(2026, 1), end=(2026, month)
            )

    async def test_rejects_a_reversed_span(self):
        with pytest.raises(ValueError, match="precedes"):
            await list_payslips(
                _client(_rows(ROW)), start=(2026, 8), end=(2025, 6)
            )

    async def test_filters_by_employee(self):
        seen: list = []
        await list_payslips(
            _client(_rows(ROW, capture=seen)), employee_ids=[119, 138], **ONE_MONTH
        )

        assert seen[0]["employeeIds"] == "119,138"


class TestOrdering:
    async def test_results_come_back_in_period_order(self):
        older = {**ROW, "id": 1, "year": 2025, "month": 6}
        newer = {**ROW, "id": 2, "year": 2026, "month": 8}
        slips = await list_payslips(
            _client(_rows(newer, older)), start=(2025, 6), end=(2026, 8)
        )

        assert [s.period for s in slips] == ["2025-06", "2026-08"]


class TestEmptyResults:
    async def test_refuses_a_silently_empty_span(self):
        """200 with no rows means "empty" or "not permitted" — indistinguishable.

        Measured in api-gaps.md §2: this endpoint returned 0 rows on a
        restricted token and 89 on a full one, HTTP 200 both times. Returning
        the empty list would hide a permission failure as a quiet month.
        """
        with pytest.raises(EmptyPayslipResult, match="may not read"):
            await payslips_or_raise(
                _client(_rows()), start=(2026, 1), end=(2026, 8)
            )

    async def test_list_payslips_reports_empty_without_complaint(self):
        """The low-level call reports what it got; the judgement lives above it."""
        assert await list_payslips(_client(_rows()), **ONE_MONTH) == []

    async def test_returns_rows_when_present(self):
        slips = await payslips_or_raise(_client(_rows(ROW)), **ONE_MONTH)
        assert len(slips) == 1
