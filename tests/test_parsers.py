"""Tests for HTML and JS parsers."""

from datetime import date
from decimal import Decimal

from tests.conftest import load_fixture
from tripletex.parsers.html import (
    extract_voucher_document_ids,
    parse_form,
    parse_remits_table,
    parse_salary_html,
    parse_wage_settings_html,
)
from tripletex.parsers.js import extract_csrf_token, extract_js_redirect_url


class TestJsParsers:
    def test_extract_csrf_token(self):
        html = load_fixture("login_page.html")
        token = extract_csrf_token(html)
        assert token == "abc123def456789012345678901234567890abcdef1234567890abcdef12345678"

    def test_extract_csrf_token_missing(self):
        assert extract_csrf_token("<html><body>no token</body></html>") is None

    def test_extract_js_redirect_url(self):
        html = load_fixture("js_redirect.html")
        url = extract_js_redirect_url(html)
        assert url == "/page/dashboard?contextId=32611682"

    def test_extract_js_redirect_url_missing(self):
        assert extract_js_redirect_url("<html><body>no redirect</body></html>") is None


class TestFormParser:
    def test_parse_form(self):
        html = load_fixture("login_page.html")
        result = parse_form(html)
        assert result is not None
        action, data = result
        assert action == "/execute/login"
        assert "username" in data
        assert "password" in data
        assert "csrfToken" in data

    def test_parse_form_missing(self):
        assert parse_form("<html><body>no form</body></html>") is None


class TestRemitsParser:
    def test_parse_remits_table(self):
        html = load_fixture("listRemits.html")
        payments = parse_remits_table(html)
        assert len(payments) == 2

        p1 = payments[0]
        assert p1.voucher == "V-12345"
        assert p1.payment_account == "1920.10.12345"
        assert p1.recipient == "Supplier AS"
        assert p1.status == "Awaiting approval"
        assert p1.due_date == date(2026, 4, 8)
        assert p1.amount == "15 000,00"

        p2 = payments[1]
        assert p2.recipient == "Another -!- Vendor"  # &#xE002; replaced
        assert p2.due_date == date(2026, 4, 5)


class TestSalaryParser:
    def test_parse_salary_html(self):
        html = load_fixture("employeeSalary.html")
        result = parse_salary_html(html)

        assert result.employee_number == "42"
        assert len(result.employments) == 2

        emp0 = result.employments[0]
        assert emp0.start_date == date(2023, 6, 15)
        assert emp0.division == "De La Casa"
        assert len(emp0.salaries) == 2
        assert emp0.salaries[0].hourly_wage == Decimal("185.50")
        assert emp0.salaries[1].hourly_wage == Decimal("195.00")
        assert emp0.salaries[0].percent_of_employment == Decimal("100")

        emp1 = result.employments[1]
        assert emp1.start_date == date(2025, 1, 1)
        assert emp1.division == "Cafe"
        assert emp1.salaries[0].yearly_wages == Decimal("487500.00")

        # Feriepenger: index 0 is deleted, index 1 is active with 12.5%
        assert result.feriepenger_rate == Decimal("12.5")


class TestWageSettingsParser:
    def test_parse_wage_settings(self):
        html = load_fixture("wageSettings.html")
        settings = parse_wage_settings_html(html)

        # Index 0 is deleted, index 1 is active
        assert settings.feriepenger_rate_1 == Decimal("10.2")
        assert settings.feriepenger_rate_2 == Decimal("12.5")
        assert settings.vacation_days == 25


class TestVoucherDocumentIds:
    def test_extract_document_ids(self):
        html = load_fixture("voucher_page.html")
        ids = extract_voucher_document_ids(html)
        assert ids == [314629598, 314629599]

    def test_extract_no_documents(self):
        ids = extract_voucher_document_ids("<html><body>no links</body></html>")
        assert ids == []
