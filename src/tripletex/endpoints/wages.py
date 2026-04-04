"""Employee salary and wage settings endpoints (HTML scraping)."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import TYPE_CHECKING

from tripletex.models import CompanyWageSettings, EmployeeSalary
from tripletex.parsers.html import parse_salary_html, parse_wage_settings_html

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def fetch_employee_list(client: TripletexClient) -> list[dict]:
    """Fetch active employee list from the salary overview API.

    POST /v2/salary/employee/overview/details
    Returns raw dicts with: id, displayName, hasResigned, number, etc.
    """
    params = {
        "from": "0",
        "fields": "id, displayName, hasResigned, number, currentCompanyEmployeeRate(id, hourlyRate, hourlyCost)",
        "count": "100",
        "employeeAvailability": "ACTIVE",
        "year": str(date.today().year),
    }

    data = await client.post_json(
        "/v2/salary/employee/overview/details",
        params=params,
        json_body={"query": "", "employeeIdsToShowOnTop": ""},
    )
    return data.get("values", [])


async def fetch_employee_salary(
    client: TripletexClient,
    employee_id: int,
) -> EmployeeSalary:
    """Fetch salary data for a single employee by parsing the HTML form.

    GET /execute/employeeSalary?employeeId=X&scope=employeeSalary&contextId=Y
    """
    context_id = client.session.context_id
    html = await client.get_html(
        "/execute/employeeSalary",
        params={
            "employeeId": str(employee_id),
            "scope": "employeeSalary",
            "contextId": context_id,
        },
    )
    return parse_salary_html(html)


async def fetch_company_wage_settings(
    client: TripletexClient,
) -> CompanyWageSettings:
    """Fetch company-level wage settings (feriepenger rates, vacation days).

    GET /execute/wageSettings?scope=wageSettingsTab&contextId=X
    """
    context_id = client.session.context_id
    html = await client.get_html(
        "/execute/wageSettings",
        params={
            "scope": "wageSettingsTab",
            "contextId": context_id,
        },
    )
    return parse_wage_settings_html(html)


async def fetch_all_wages(
    client: TripletexClient,
    delay: float = 0.3,
) -> dict:
    """Fetch wage data for all active employees + company settings.

    Returns dict with 'companySettings' and 'employees' keys,
    matching the format of ../Tamigo/data/tripletex_wages.json.
    """
    company_settings = await fetch_company_wage_settings(client)
    employees = await fetch_employee_list(client)

    results = []
    for emp in employees:
        try:
            salary_data = await fetch_employee_salary(client, emp["id"])
            results.append({
                "tripletexId": emp["id"],
                "employeeNumber": emp.get("number"),
                "displayName": emp.get("displayName"),
                **salary_data.model_dump(mode="json"),
            })
            await asyncio.sleep(delay)
        except Exception as e:
            results.append({
                "tripletexId": emp["id"],
                "employeeNumber": emp.get("number"),
                "displayName": emp.get("displayName"),
                "error": str(e),
            })

    return {
        "companySettings": company_settings.model_dump(mode="json"),
        "employees": results,
    }
