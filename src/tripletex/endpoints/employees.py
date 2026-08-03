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

from tripletex.models import Employee, EmployeeAccess, EmployeeOverview
from tripletex.parsers.html import parse_employee_privileges_html

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
    count: int = 1000,
) -> list[Employee]:
    """GET /v2/employee. Availability: ACTIVE, INACTIVE or ALL."""
    params: dict[str, str] = {
        "from": "0",
        "count": str(count),
        "employeeAvailability": availability,
    }
    if query:
        params["query"] = query
    if fields:
        params["fields"] = fields
    data = await client.get_json("/v2/employee", params=params)
    return [Employee.model_validate(v) for v in data.get("values", [])]


async def get_employee(
    client: TripletexClient,
    employee_id: int,
    fields: str = _EMPLOYEE_FIELDS,
) -> Employee:
    """GET /v2/employee/{id}"""
    params = {"fields": fields} if fields else {}
    data = await client.get_json(f"/v2/employee/{employee_id}", params=params)
    return Employee.model_validate(data.get("value", data))


def _require_web_session(client: TripletexClient, what: str) -> None:
    from tripletex.session import WebSession

    if not isinstance(client.session, WebSession):
        raise RuntimeError(f"{what} requires web session auth (use --auth web)")


async def fetch_employee_overview(
    client: TripletexClient,
    availability: str = "ALL",
    fields: str = _OVERVIEW_FIELDS,
    count: int = 1000,
) -> list[EmployeeOverview]:
    """POST /v2/salary/employee/overview/details (internal — web session only).

    Carries payslip delivery method and `allowLogin`, neither of which the public
    API exposes. API tokens get a 403 from this endpoint.
    """
    _require_web_session(client, "the salary employee overview")

    data = await client.post_json(
        "/v2/salary/employee/overview/details",
        params={
            "fields": fields,
            "from": "0",
            "count": str(count),
            "sorting": "displayName",
            "employeeAvailability": availability,
        },
        json_body={"query": "", "employeeIdsToShowOnTop": ""},
    )
    return [EmployeeOverview.model_validate(v) for v in data.get("values", [])]


async def fetch_employee_access(
    client: TripletexClient,
    employee_id: int,
) -> EmployeeAccess:
    """Fetch login access settings by parsing the "User access" tab (web session).

    GET /execute/updateEmployeePrivileges?employeeId=X&scope=updateEmployeePrivileges
    """
    _require_web_session(client, "employee access")

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
