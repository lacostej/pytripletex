"""HTML parsing utilities for Tripletex pages using BeautifulSoup."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from tripletex.models import (
    CompanyWageSettings,
    Employment,
    EmployeeSalary,
    Payment,
    SalaryEntry,
)


def parse_form(html: str, form_selector: str = "form") -> tuple[str, dict[str, str]] | None:
    """Extract action URL and input fields from an HTML form.

    Returns (action_url, {name: value}) or None if no form found.
    """
    soup = BeautifulSoup(html, "lxml")
    form = soup.select_one(form_selector)
    if form is None:
        return None

    action = form.get("action", "")
    data: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name") or inp.get("id")
        if name:
            data[name] = inp.get("value", "")
    return action, data


def parse_remits_table(html: str) -> list[Payment]:
    """Parse the payment remittances HTML table from /execute/listRemits.

    Handles the &#xE002; character that Tripletex uses.
    """
    soup = BeautifulSoup(html, "lxml")

    rows = soup.select("form > table > tbody > tr")
    payments: list[Payment] = []

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        voucher_el = cells[1].select_one("div div span a")
        voucher = voucher_el.get_text(strip=True) if voucher_el else ""

        payment_account = cells[2].get_text(strip=True)

        raw_recipient = cells[3].get_text(strip=True).replace("\ue002", "-!- ")
        recipient = re.sub(r"\s+", " ", raw_recipient)

        status_el = cells[4].find(string=True, recursive=False) or cells[4].get_text(strip=True)
        status = str(status_el).strip() if status_el else ""

        due_date_el = cells[5].select_one("div span a")
        due_date_str = due_date_el.get_text(strip=True) if due_date_el else ""
        try:
            due = date.fromisoformat(due_date_str)
        except ValueError:
            continue

        amount_el = cells[6].select_one("span")
        amount = amount_el.get_text(strip=True) if amount_el else ""

        payments.append(
            Payment(
                voucher=voucher,
                payment_account=payment_account,
                recipient=recipient,
                status=status,
                due_date=due,
                amount=amount,
            )
        )

    return payments


def _parse_number(s: str) -> Decimal | None:
    """Parse Norwegian number format: '487,500' or '487.500,50' or '487500.50'."""
    s = s.strip()
    if not s:
        return None
    # Remove thousand separators (dots when comma is decimal sep)
    # If there's a comma, treat it as decimal separator
    if "," in s:
        # e.g. "487.500,50" -> "487500.50"
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _escape_re(s: str) -> str:
    return re.escape(s)


def parse_salary_html(html: str) -> EmployeeSalary:
    """Parse the /execute/employeeSalary HTML form into structured data.

    Extracts employments, salaries, and per-employee feriepenger override
    from the form input fields following the pattern:
      sortedEmployments[N].guiSalaries[M].fieldName
    """
    soup = BeautifulSoup(html, "lxml")
    inputs = {
        inp.get("name", ""): inp.get("value", "")
        for inp in soup.find_all("input")
        if inp.get("name")
    }
    # Also capture data-display-name attributes
    display_names: dict[str, str] = {}
    for inp in soup.find_all("input"):
        name = inp.get("name", "")
        dn = inp.get("data-display-name")
        if name and dn:
            display_names[name] = dn

    # Employee number
    employee_number = inputs.get("employee.number")

    # Find employment indices
    emp_indices: set[int] = set()
    for name in inputs:
        m = re.match(r"sortedEmployments\[(\d+)\]", name)
        if m:
            emp_indices.add(int(m.group(1)))

    employments: list[Employment] = []
    for emp_idx in sorted(emp_indices):
        prefix = f"sortedEmployments[{emp_idx}]"

        start_date_str = inputs.get(f"{prefix}.startDate")
        start_date = None
        if start_date_str:
            try:
                start_date = date.fromisoformat(start_date_str)
            except ValueError:
                pass

        div_key = f"{prefix}.divisionId"
        division = display_names.get(div_key)

        # Find salary indices
        sal_indices: set[int] = set()
        sal_pattern = re.compile(rf"{_escape_re(prefix)}\.guiSalaries\[(\d+)\]")
        for name in inputs:
            m = sal_pattern.match(name)
            if m:
                sal_indices.add(int(m.group(1)))

        salaries: list[SalaryEntry] = []
        for sal_idx in sorted(sal_indices):
            sal_prefix = f"{prefix}.guiSalaries[{sal_idx}]"

            sal_date_str = inputs.get(f"{sal_prefix}.date")
            sal_date = None
            if sal_date_str:
                try:
                    sal_date = date.fromisoformat(sal_date_str)
                except ValueError:
                    pass

            yearly_str = inputs.get(f"{sal_prefix}.yearlyWages", "")
            hourly_str = inputs.get(f"{sal_prefix}.hourlyWage", "")
            pct_str = inputs.get(f"{sal_prefix}.percentOfEmployment", "")

            salaries.append(
                SalaryEntry(
                    date=sal_date,
                    yearly_wages=_parse_number(yearly_str),
                    hourly_wage=_parse_number(hourly_str),
                    percent_of_employment=_parse_number(pct_str),
                )
            )

        salaries.sort(key=lambda s: s.date or date.min)

        employments.append(
            Employment(
                index=emp_idx,
                start_date=start_date,
                division=division,
                salaries=salaries,
            )
        )

    # Per-employee feriepenger override
    feriepenger_rate: Decimal | None = None
    vac_indices: set[int] = set()
    for name in inputs:
        m = re.match(r"employee\.guiEmployeeVacations\[(\d+)\]", name)
        if m:
            vac_indices.add(int(m.group(1)))

    for vac_idx in sorted(vac_indices):
        vac_prefix = f"employee.guiEmployeeVacations[{vac_idx}]"
        deleted = inputs.get(f"{vac_prefix}.deleted", "")
        if deleted == "true":
            continue
        pct1_str = inputs.get(f"{vac_prefix}.vacationPayPercentage1", "")
        pct = _parse_number(pct1_str)
        if pct and pct > 0:
            feriepenger_rate = pct

    return EmployeeSalary(
        employee_number=employee_number,
        employments=employments,
        feriepenger_rate=feriepenger_rate,
    )


def parse_wage_settings_html(html: str) -> CompanyWageSettings:
    """Parse /execute/wageSettings HTML form for company-level vacation pay rates."""
    soup = BeautifulSoup(html, "lxml")
    inputs = {
        inp.get("name", ""): inp.get("value", "")
        for inp in soup.find_all("input")
        if inp.get("name")
    }

    result = CompanyWageSettings()

    vac_indices: set[int] = set()
    for name in inputs:
        m = re.match(r"companyVacations\[(\d+)\]", name)
        if m:
            vac_indices.add(int(m.group(1)))

    for idx in sorted(vac_indices):
        prefix = f"companyVacations[{idx}]"
        deleted = inputs.get(f"{prefix}.deleted", "")
        if deleted == "true":
            continue
        year_str = inputs.get(f"{prefix}.year", "0")
        if not year_str or year_str == "0":
            continue

        pct1 = _parse_number(inputs.get(f"{prefix}.vacationPayPercentage1", ""))
        pct2 = _parse_number(inputs.get(f"{prefix}.vacationPayPercentage2", ""))
        days_str = inputs.get(f"{prefix}.days", "")

        if pct1 is not None:
            result.feriepenger_rate_1 = pct1
        if pct2 is not None:
            result.feriepenger_rate_2 = pct2
        if days_str:
            try:
                result.vacation_days = int(days_str)
            except ValueError:
                pass

    return result


def extract_voucher_document_ids(html: str) -> list[int]:
    """Extract document IDs from a voucher page.

    Looks for links with viewerDocument(ID) pattern, as in browser.rb:79.
    """
    soup = BeautifulSoup(html, "lxml")
    doc_ids: list[int] = []

    for link in soup.find_all("a", class_="linkFunction"):
        href = link.get("href", "")
        m = re.search(r"viewerDocument\((\d+)\)", href)
        if m:
            doc_ids.append(int(m.group(1)))

    # Also check onclick attributes
    for link in soup.find_all("a", onclick=True):
        m = re.search(r"viewerDocument\((\d+)\)", link["onclick"])
        if m:
            doc_ids.append(int(m.group(1)))

    return doc_ids
