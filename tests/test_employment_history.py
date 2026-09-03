"""Wage history from the API, replacing an HTML scrape.

`/execute/employeeSalary` had to be regexed field by field, its numbers parsed
out of Norwegian formatting, and a blank "new salary" template row skipped.
`/v2/employee/employment` carries the same history as data — plus monthly
salary, remuneration type, occupation code and the division's organisation
number, none of which the HTML exposed.

The rows below are real, from Bonita Handel 2026-09-03.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.employees import (
    list_employments,
    list_holiday_settings,
    list_leave_of_absence,
)
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

EMPLOYMENT = {
    "id": 1032266,
    "startDate": "2021-01-01",
    "endDate": None,
    "isMainEmployer": True,
    "employee": {"id": 4888742, "firstName": "Rahman", "employeeNumber": "12"},
    "division": {
        "id": 37714290,
        "name": "BONITA CAFE BRISKEBY",
        "organizationNumber": "914415047",
    },
    "employmentDetails": [
        {"id": 1, "date": "2021-01-01", "annualSalary": 390000.0, "hourlyWage": 200.0,
         "percentageOfFullTimeEquivalent": 20.0, "remunerationType": "HOURLY_WAGE"},
        {"id": 2, "date": "2023-08-11", "annualSalary": 390000.0, "hourlyWage": 200.0,
         "percentageOfFullTimeEquivalent": 70.0, "remunerationType": "HOURLY_WAGE"},
        {"id": 3, "date": "2024-06-01", "annualSalary": 399750.0, "hourlyWage": 205.0,
         "percentageOfFullTimeEquivalent": 70.0, "remunerationType": "HOURLY_WAGE"},
        {"id": 4, "date": "2026-05-01", "annualSalary": 450450.0, "hourlyWage": 231.0,
         "percentageOfFullTimeEquivalent": 70.0, "remunerationType": "HOURLY_WAGE"},
    ],
}


def _client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _by_employee(employees: dict[int, list]):
    """Serve employments per employeeId, and *one* row when unfiltered — which
    is what the real endpoint does."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/employee":
            rows = [{"id": i} for i in employees]
        elif "employeeId" in request.url.params:
            rows = employees.get(int(request.url.params["employeeId"]), [])
        else:
            # The quirk: 200, one row, no warning.
            rows = [next(iter(v), None) for v in employees.values()][:1]
            rows = [r for r in rows if r]
        frm = int(request.url.params.get("from", 0))
        cnt = int(request.url.params.get("count", 1000))
        page = rows[frm : frm + cnt]
        return httpx.Response(200, json={"values": page, "fullResultSize": len(page)})

    return handler


class TestSalaryHistory:
    async def test_history_comes_back_whole(self):
        (e,) = await list_employments(_client(_by_employee({4888742: [EMPLOYMENT]})))

        assert len(e.salary_history) == 4
        assert [r.annual_salary for r in e.salary_history] == [
            Decimal("390000"), Decimal("390000"),
            Decimal("399750"), Decimal("450450"),
        ]

    async def test_row_fields_map_to_the_scraped_names(self):
        (e,) = await list_employments(_client(_by_employee({4888742: [EMPLOYMENT]})))
        first = e.salary_history[0]

        assert first.date == datetime.date(2021, 1, 1)
        assert first.annual_salary == Decimal("390000")   # was yearlyWages
        assert first.hourly_wage == Decimal("200")        # was hourlyWage
        assert first.percentage_of_full_time == Decimal("20")  # was percentOfEmployment
        assert first.remuneration_type == "HOURLY_WAGE"   # not in the HTML at all

    async def test_salary_on_a_date_takes_the_row_in_force(self):
        (e,) = await list_employments(_client(_by_employee({4888742: [EMPLOYMENT]})))

        assert e.salary_on(datetime.date(2024, 7, 1)).annual_salary == Decimal("399750")
        assert e.salary_on(datetime.date(2023, 1, 1)).annual_salary == Decimal("390000")

    async def test_salary_before_the_first_row_is_none(self):
        (e,) = await list_employments(_client(_by_employee({4888742: [EMPLOYMENT]})))

        assert e.salary_on(datetime.date(2019, 1, 1)) is None

    async def test_future_dated_rows_are_kept(self):
        """A raise already agreed for 2026-05-01 is history the moment it is
        entered, and dropping it would understate the wage bill."""
        (e,) = await list_employments(_client(_by_employee({4888742: [EMPLOYMENT]})))

        assert e.salary_on(datetime.date(2026, 6, 1)).annual_salary == Decimal("450450")

    async def test_org_number_is_a_field_not_a_regex(self):
        """The scrape pulled it out of a display name with `\\((\\d{6,})\\)`."""
        (e,) = await list_employments(_client(_by_employee({4888742: [EMPLOYMENT]})))

        assert e.division_organization_number == "914415047"
        assert e.division_name == "BONITA CAFE BRISKEBY"


class TestThePerEmployeeQuirk:
    async def test_every_employee_is_asked_for_separately(self):
        """Called without employeeId the endpoint answers 200 with a single row
        — 1 instead of 81 on a real company, silently. Trusting the unfiltered
        list yields a plausible, tiny, wrong answer."""
        second = {**EMPLOYMENT, "id": 999, "employee": {"id": 4888743}}
        employments = await list_employments(
            _client(_by_employee({4888742: [EMPLOYMENT], 4888743: [second]}))
        )

        assert len(employments) == 2

    async def test_a_caller_supplying_ids_skips_the_employee_fetch(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_employments(_client(handler), employee_ids=[1, 2])

        assert "/v2/employee" not in seen
        assert seen.count("/v2/employee/employment") == 2

    async def test_employee_id_is_actually_sent(self):
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await list_employments(_client(handler), employee_ids=[4888742])

        assert seen[0].params["employeeId"] == "4888742"


class TestLeaveAndHolidays:
    async def test_leave_rows_parse(self):
        row = {"id": 142595, "employment": {"id": 1032266},
               "startDate": "2025-02-14", "endDate": "2025-05-25",
               "percentage": 100.0, "type": "FURLOUGH", "isWageDeduction": False}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"values": [row], "fullResultSize": 1})

        (loa,) = await list_leave_of_absence(_client(handler))

        assert loa.type == "FURLOUGH"
        assert loa.percentage == Decimal("100")
        assert loa.start_date == datetime.date(2025, 2, 14)
        assert not loa.is_wage_deduction

    async def test_holiday_settings_include_the_over_60_rate(self):
        """`vacationPayPercentage2` is the over-60 rate, which the HTML scrape
        never picked up."""
        row = {"id": 213912, "year": 1970, "days": 25.0,
               "vacationPayPercentage1": 12.0, "vacationPayPercentage2": 14.3,
               "isMaxPercentage2Amount6G": True}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"values": [row], "fullResultSize": 1})

        (h,) = await list_holiday_settings(_client(handler))

        assert h.vacation_pay_percentage == Decimal("12")
        assert h.vacation_pay_percentage_2 == Decimal("14.3")
        assert h.is_max_percentage_2_amount_6g
