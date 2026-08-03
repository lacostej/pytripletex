"""Tests for model-level logic (employment status, login access, payslip delivery)."""

from datetime import date

from tripletex.models import Employee, EmployeeAccess, EmployeeOverview


def _employee(**employments) -> Employee:
    return Employee.model_validate(
        {
            "id": 1,
            "displayName": "Test Employee",
            "employments": list(employments.values()),
        }
    )


class TestEmploymentStatus:
    def test_open_ended_employment_is_active(self):
        e = _employee(current={"id": 1, "startDate": "2026-05-01", "endDate": None})
        assert e.has_active_employment(date(2026, 8, 3)) is True

    def test_end_date_is_inclusive(self):
        e = _employee(past={"id": 1, "startDate": "2020-01-01", "endDate": "2026-04-30"})
        assert e.has_active_employment(date(2026, 4, 30)) is True
        assert e.has_active_employment(date(2026, 5, 1)) is False

    def test_future_employment_is_not_active_yet(self):
        e = _employee(future={"id": 1, "startDate": "2026-09-01", "endDate": None})
        assert e.has_active_employment(date(2026, 8, 3)) is False

    def test_unit_change_leaves_one_active_period(self):
        # What a unit change looks like: old period ended, new one opened.
        e = _employee(
            old={
                "id": 1,
                "startDate": "2021-01-01",
                "endDate": "2026-04-30",
                "employmentEndReason": "EMPLOYMENT_END_INTERNAL_CHANGE",
                "isRemoveAccessAtEmploymentEnded": True,
                "division": {"id": 1, "name": "Old unit"},
            },
            new={
                "id": 2,
                "startDate": "2026-05-01",
                "endDate": None,
                "division": {"id": 2, "name": "New unit"},
            },
        )
        on = date(2026, 8, 3)
        active = e.active_employments(on)
        assert [p.division_name for p in active] == ["New unit"]
        assert e.latest_employment.id == 2
        assert e.employments[0].removes_access_at_end is True


class TestEmployeeAccess:
    def test_login_disabled_means_access_ended(self):
        access = EmployeeAccess(employee_id=1, allow_login=False)
        assert access.access_ended(date(2026, 8, 3)) is True

    def test_no_end_date_means_access_open(self):
        access = EmployeeAccess(employee_id=1, allow_login=True)
        assert access.access_ended(date(2026, 8, 3)) is False

    def test_end_date_is_inclusive(self):
        access = EmployeeAccess(
            employee_id=1, allow_login=True, login_end_date=date(2026, 4, 30)
        )
        assert access.access_ended(date(2026, 4, 30)) is False
        assert access.access_ended(date(2026, 5, 1)) is True


class TestPayslipDelivery:
    def test_app_delivery_english_and_norwegian(self):
        for value in ("The Tripletex app", "Tripletex-appen"):
            row = EmployeeOverview.model_validate(
                {"id": 1, "deliveryMethodWageSlipString": value}
            )
            assert row.payslip_via_app is True

    def test_manual_delivery_english_and_norwegian(self):
        for value in ("Manual handling", "Manuell håndtering", None):
            row = EmployeeOverview.model_validate(
                {"id": 1, "deliveryMethodWageSlipString": value}
            )
            assert row.payslip_via_app is False
