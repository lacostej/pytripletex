"""Employee endpoints (official API), plus login-access scraping.

Employments come from `/v2/employee` and work with both auth modes. The login
access settings (`allowLogin`, `loginEndDate`) are not exposed by any `/v2/*`
endpoint — the internal `/v2/salary/employee/overview/details` carries
`allowLogin` but rejects API tokens with 403 — so they are scraped from the
employee's "User access" tab, which requires a web session.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from tripletex.endpoints._paging import paginate
from tripletex.models import (
    Employee,
    EmployeeAccess,
    EmployeeOverview,
    EmploymentPeriod,
    HolidaySettings,
    LeaveOfAbsence,
)
from tripletex.parsers.html import parse_employee_privileges_html
from tripletex.session import require_web_session

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


# `fields=*` collapses employments to nothing, so ask for them explicitly.
_EMPLOYEE_FIELDS = (
    "id,firstName,lastName,displayName,employeeNumber,email,"
    "department(id,name),"
    "employments(id,startDate,endDate,employmentEndReason,isMainEmployer,"
    "isRemoveAccessAtEmploymentEnded,division(id,name))"
)

_OVERVIEW_FIELDS = (
    "id,displayName,number,deliveryMethodWageSlipString,allowLogin,hasResigned"
)


async def list_employees(
    client: TripletexClient,
    query: str | None = None,
    availability: str = "ALL",
    fields: str = _EMPLOYEE_FIELDS,
    limit: int | None = None,
) -> list[Employee]:
    """GET /v2/employee. Availability: ACTIVE, INACTIVE or ALL.

    Returns every match unless `limit` is given.
    """
    params: dict[str, str] = {"employeeAvailability": availability}
    if query:
        params["query"] = query
    if fields:
        params["fields"] = fields
    values = await paginate(client, "/v2/employee", params=params, limit=limit)
    return [Employee.model_validate(v) for v in values]


async def get_employee(
    client: TripletexClient,
    employee_id: int,
    fields: str = _EMPLOYEE_FIELDS,
) -> Employee:
    """GET /v2/employee/{id}"""
    params = {"fields": fields} if fields else {}
    data = await client.get_json(f"/v2/employee/{employee_id}", params=params)
    return Employee.model_validate(data.get("value", data))


async def fetch_employee_overview(
    client: TripletexClient,
    availability: str = "ALL",
    fields: str = _OVERVIEW_FIELDS,
    limit: int | None = None,
) -> list[EmployeeOverview]:
    """POST /v2/salary/employee/overview/details (internal — web session only).

    Carries payslip delivery method and `allowLogin`, neither of which the public
    API exposes. API tokens get a 403 from this endpoint.
    """
    require_web_session(client.session, "The salary employee overview")

    values = await paginate(
        client,
        "/v2/salary/employee/overview/details",
        params={
            "fields": fields,
            "sorting": "displayName",
            "employeeAvailability": availability,
        },
        json_body={"query": "", "employeeIdsToShowOnTop": ""},
        limit=limit,
    )
    return [EmployeeOverview.model_validate(v) for v in values]


async def fetch_employee_access(
    client: TripletexClient,
    employee_id: int,
) -> EmployeeAccess:
    """Fetch login access settings by parsing the "User access" tab (web session).

    GET /execute/updateEmployeePrivileges?employeeId=X&scope=updateEmployeePrivileges
    """
    require_web_session(client.session, "Employee login access")

    html = await client.get_html(
        "/execute/updateEmployeePrivileges",
        params={
            "employeeId": str(employee_id),
            "scope": "updateEmployeePrivileges",
            "contextId": client.session.context_id,
        },
    )
    return parse_employee_privileges_html(html, employee_id)


async def fetch_access_report(
    client: TripletexClient,
    employees: list[Employee],
) -> list[tuple[Employee, EmployeeAccess]]:
    """Fetch access settings for each employee (one page request per employee)."""
    report: list[tuple[Employee, EmployeeAccess]] = []
    for employee in employees:
        if employee.id is None:
            continue
        report.append((employee, await fetch_employee_access(client, employee.id)))
    return report


def find_access_issues(
    report: list[tuple[Employee, EmployeeAccess]],
    on: date | None = None,
) -> list[tuple[Employee, EmployeeAccess]]:
    """Employees with an active employment whose login access has ended.

    This is the fallout of changing an employee's unit: Tripletex ends the old
    employment (`EMPLOYMENT_END_INTERNAL_CHANGE`) and, when that employment has
    `isRemoveAccessAtEmploymentEnded` set, revokes the login without restoring it
    for the new employment.
    """
    return [
        (employee, access)
        for employee, access in report
        if employee.has_active_employment(on) and access.access_ended(on)
    ]


# The full salary history hangs off the employment, so ask for it inline rather
# than following `employmentDetails` per row.
_EMPLOYMENT_FIELDS = (
    "id,employmentId,startDate,endDate,employmentEndReason,isMainEmployer,"
    "taxDeductionCode,lastSalaryChangeDate,"
    "employee(id,firstName,lastName,employeeNumber),"
    "division(id,name,organizationNumber),"
    "employmentDetails(id,date,annualSalary,monthlySalary,hourlyWage,"
    "percentageOfFullTimeEquivalent,employmentType,employmentForm,"
    "remunerationType,workingHoursScheme,shiftDurationHours,"
    "occupationCode(id,nameNO))"
)


async def list_employments(
    client: TripletexClient,
    employee_ids: list[int] | None = None,
    availability: str = "ALL",
) -> list[EmploymentPeriod]:
    """Every employment with its full salary history.

    GET /v2/employee/employment, once per employee.

    **The per-employee loop is not an optimisation choice — it is required.**
    Called without `employeeId` the endpoint answers 200 with a *single* row
    rather than all of them: 1 instead of 81 on Bonita Handel, no error and no
    warning. It is the silent-filtering shape described in `api-gaps.md` §2, and
    a caller that trusts the unfiltered list gets a plausible, tiny, wrong
    answer. So the employee list is fetched first and each id asked for
    separately — 65 requests for 81 employments, comfortable inside the rate
    limit.

    This replaces scraping `/execute/employeeSalary`, which had to regex each
    field out of form inputs, parse Norwegian decimals, and skip the blank
    "new salary" row Tripletex renders. It also reaches things the HTML never
    exposed: `monthlySalary`, `remunerationType`, `occupationCode`,
    `taxDeductionCode`, and the division's `organizationNumber` as a field
    rather than a number scraped out of a display name.
    """
    if employee_ids is None:
        employees = await list_employees(
            client, availability=availability, fields="id"
        )
        employee_ids = [e.id for e in employees if e.id is not None]

    employments: list[EmploymentPeriod] = []
    for employee_id in employee_ids:
        values = await paginate(
            client,
            "/v2/employee/employment",
            params={"employeeId": str(employee_id), "fields": _EMPLOYMENT_FIELDS},
        )
        employments.extend(EmploymentPeriod.model_validate(v) for v in values)
    return employments


async def list_leave_of_absence(
    client: TripletexClient,
    limit: int | None = None,
) -> list[LeaveOfAbsence]:
    """Leave periods across all employments.

    GET /v2/employee/employment/leaveOfAbsence. Unlike `list_employments`, this
    one does return the whole set without a per-employee filter.
    """
    values = await paginate(
        client,
        "/v2/employee/employment/leaveOfAbsence",
        params={
            "fields": "id,employment(id),startDate,endDate,percentage,type,"
            "isWageDeduction"
        },
        limit=limit,
    )
    return [LeaveOfAbsence.model_validate(v) for v in values]


async def list_holiday_settings(client: TripletexClient) -> list[HolidaySettings]:
    """Vacation days and pay percentages, per year.

    GET /v2/salary/settings/holiday — the API equivalent of the
    `/execute/wageSettings` scrape, and it carries the over-60 rate
    (`vacationPayPercentage2`) that the HTML version never picked up.

    Years are sparse: a row applies until the next one supersedes it, and the
    baseline row is dated 1970.
    """
    values = await paginate(
        client,
        "/v2/salary/settings/holiday",
        params={
            "fields": "id,year,days,vacationPayPercentage1,vacationPayPercentage2,"
            "isMaxPercentage2Amount6G"
        },
    )
    return [HolidaySettings.model_validate(v) for v in values]
