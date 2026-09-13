"""Salary drafts: the lifecycle, and what must be true before committing one.

Payloads captured from the Tripletex UI 2026-09-04, importing a real August run
for the smaller company (transaction 7562010, nine payslips, 110 602.81 NOK).

The point of this module is that `/v2/tsk/salaryv2/*` has a draft and
`/v2/salary/transaction` does not. These tests pin the distinction, because
getting it wrong means booking a voucher when you meant to stage one.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints import salary
from tripletex.session import ApiSession, WebSession, WebSessionRequired

BASE_URL = "https://tripletex.no"

_SPECS = [
    {"id": 228713809, "specificationType": "TYPE_MANUAL", "rate": 12675.0,
     "count": 1.0, "amount": 12675.0, "description": "Accounting",
     "countAndRateEditable": True,
     "salaryType": {"id": 28924495, "number": "2000", "name": "Fastlønn"}},
    {"id": 228713810, "specificationType": "TYPE_TAX", "rate": 0.0, "count": 0.0,
     "amount": -3802.0, "countAndRateEditable": False,
     "description": "Basis amount percentage deduction (30.0%): 12,675.00.",
     "salaryType": {"id": 28924506, "number": "6000", "name": "Skattetrekk"}},
]

PAYMENT = {
    "id": 35980111, "grossAmount": 12675.00, "amount": 8873.00,
    "isTaxCardMissing": False, "payrollTaxPercentage": 14.1,
    "employee": {"id": 8988551, "displayName": "Kari Nordmann",
                 "employeeNumber": "100"},
    "specifications": _SPECS, "travelExpenses": [],
    "validationResults": {"warnings": [], "validations": [], "poisonPills": []},
}

DRAFT = {
    "id": 7562010, "displayName": "Salary payment (under process)",
    "date": "2026-09-04", "year": 2026, "month": 8, "periodAsString": "August 2026",
    "completed": False, "voucher": None, "paymentDate": None,
    "paySlipsAvailableDate": "2026-09-04", "isHistorical": False,
    "voucherComment": "Odins", "sumPaidAmount": 110602.81,
    "sumTaxDeductionAmount": -34412.00, "salaryPayments": [PAYMENT],
}

COMMITTED = {
    **DRAFT, "displayName": "Salary voucher 14-2026", "completed": True,
    "paymentDate": "2026-09-04", "voucher": {"id": 676496151},
}


def _web(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = WebSession(cookies=httpx.Cookies(), context_id="56801690")
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _api(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _value(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": payload})

    return handler


class TestDraftLifecycle:
    async def test_an_uncommitted_run_is_a_draft(self):
        draft = await salary.get_salary_draft(_web(_value(DRAFT)), 7562010)

        assert not draft.is_committed
        assert draft.voucher is None
        assert draft.display_name == "Salary payment (under process)"

    async def test_a_committed_run_carries_a_voucher(self):
        draft = await salary.get_salary_draft(_web(_value(COMMITTED)), 7562010)

        assert draft.is_committed
        assert draft.voucher["id"] == 676496151
        assert draft.display_name == "Salary voucher 14-2026"

    async def test_is_committed_reads_completed_not_just_the_voucher(self):
        """A draft carries `voucher: null` rather than omitting it, so testing
        the key's presence would call every draft committed."""
        draft = await salary.get_salary_draft(_web(_value(DRAFT)), 7562010)

        assert "voucher" in DRAFT
        assert not draft.is_committed

    async def test_totals_and_period_parse(self):
        draft = await salary.get_salary_draft(_web(_value(DRAFT)), 7562010)

        assert draft.sum_paid == Decimal("110602.81")
        assert draft.sum_tax_deduction == Decimal("-34412.00")
        assert draft.year == 2026 and draft.month == 8


class TestPreflight:
    async def test_manual_and_tax_lines_are_distinguished(self):
        """TYPE_MANUAL is what you imported; TYPE_TAX is what Tripletex worked
        out. The tax line is not editable, because it is a result."""
        draft = await salary.get_salary_draft(_web(_value(DRAFT)), 7562010)
        manual, tax = draft.salary_payments[0].specifications

        assert not manual.is_tax and manual.count_and_rate_editable
        assert tax.is_tax and not tax.count_and_rate_editable
        assert tax.amount == Decimal("-3802")

    async def test_missing_tax_card_is_surfaced_before_the_run(self):
        """Missing card means 50% withholding — worth catching while the run is
        still editable."""
        payload = {**DRAFT, "salaryPayments": [{**PAYMENT, "isTaxCardMissing": True}]}
        draft = await salary.get_salary_draft(_web(_value(payload)), 7562010)

        assert len(draft.employees_without_tax_card) == 1

    async def test_clean_draft_has_no_blocking_problems(self):
        draft = await salary.get_salary_draft(_web(_value(DRAFT)), 7562010)

        assert draft.blocking_problems == []

    async def test_poison_pills_are_blocking(self):
        payload = {**DRAFT, "salaryPayments": [{
            **PAYMENT,
            "validationResults": {"warnings": [], "validations": [],
                                  "poisonPills": [{"message": "no bank account"}]},
        }]}
        draft = await salary.get_salary_draft(_web(_value(payload)), 7562010)

        assert len(draft.blocking_problems) == 1

    async def test_warnings_do_not_block(self):
        payload = {**DRAFT, "salaryPayments": [{
            **PAYMENT,
            "validationResults": {"warnings": [{"message": "unusual hours"}],
                                  "validations": [], "poisonPills": []},
        }]}
        draft = await salary.get_salary_draft(_web(_value(payload)), 7562010)

        assert draft.blocking_problems == []
        assert len(draft.salary_payments[0].warnings) == 1


class TestImport:
    async def test_upload_returns_the_handle_the_import_needs(self):
        uploaded = [{"id": "1164475805", "revision": "1",
                     "name": "Payroll.csv", "size": "4484",
                     "checksum": "40031ec8c0bf11f3d3292448e6c04a1bcd77def0"}]

        def handler(request: httpx.Request) -> httpx.Response:
            assert b"importFile" in request.content
            return httpx.Response(200, json=uploaded)

        got = await salary.upload_import_file(_web(handler), __file__)

        assert got.id == "1164475805"
        assert got.checksum.startswith("40031ec")

    async def test_transaction_id_is_dug_out_of_the_forward_instruction(self):
        """The new draft's id arrives only inside a UI navigation string —
        there is no field for it."""
        forward = json.dumps([{"_action": "navigateDirect",
                               "url": "/execute/salary?salaryTransactionId=7562010"}])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"result": {"forward": forward}})

        got = await salary.import_salary_csv(
            _web(handler),
            salary.ImportedFile(id="1", name="p.csv", checksum="c"),
            voucher_date=datetime.date(2026, 9, 4),
        )

        assert got == 7562010

    async def test_import_switches_are_sent(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"forward": json.dumps(
                [{"url": "/execute/salary?salaryTransactionId=1"}])}})

        await salary.import_salary_csv(
            _web(handler), salary.ImportedFile(id="9", name="p.csv", checksum="c"),
            voucher_date=datetime.date(2026, 9, 4),
            ignore_first_row=True, generate_tax_deduction=True, is_historical=False,
        )
        args = seen["body"]["params"][0]

        assert args["ignoreFirstRow"] is True
        assert args["generateTaxDeduction"] is True
        assert args["isHistorical"] is False
        assert args["voucherDate"] == "2026-09-04"
        assert seen["body"]["params"][1] == "doImport"

    async def test_an_import_that_returns_no_id_raises_with_the_validations(self):
        """Silence here would mean a caller polling a draft that never existed."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"result": {
                "forward": None, "validations": ["Unknown employee number 999"]}})

        with pytest.raises(RuntimeError, match="Unknown employee number 999"):
            await salary.import_salary_csv(
                _web(handler), salary.ImportedFile(id="1", name="p.csv", checksum="c"),
                voucher_date=datetime.date(2026, 9, 4),
            )


class TestCommitting:
    async def test_run_sends_the_dates_and_payment_type(self):
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"value": COMMITTED})

        result = await salary.run_salary(
            _web(handler), 7562010,
            payment_type_id=-3,
            payment_date=datetime.date(2026, 9, 4),
            payslips_available_date=datetime.date(2026, 9, 10),
        )

        assert seen[0].path.endswith("/transaction/7562010/:run")
        assert seen[0].params["paymentTypeId"] == "-3"
        assert seen[0].params["paymentDate"] == "2026-09-04"
        # Set at run time, not at import — visibility can lag the booking.
        assert seen[0].params["paySlipsAvailableDate"] == "2026-09-10"
        assert result.is_committed

    async def test_run_arguments_are_keyword_only(self):
        with pytest.raises(TypeError):
            await salary.run_salary(_web(_value(COMMITTED)), 7562010, -3)

    async def test_add_calculated_specs_targets_named_payments(self):
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"value": DRAFT})

        await salary.add_calculated_specs(
            _web(handler), 7562010, payment_ids=[35980111, 35980112]
        )

        assert seen[0].params["salaryPaymentIds"] == "35980111,35980112"
        assert seen[0].params["specTypesToAdd"] == "EXPENSES"

    async def test_add_specs_refuses_an_empty_target(self):
        with pytest.raises(ValueError, match="nothing to add"):
            await salary.add_calculated_specs(
                _web(_value(DRAFT)), 7562010, payment_ids=[]
            )


class TestAuth:
    """`/v2/tsk/salaryv2/*` answers 403 to an API token, so fail with an
    instruction rather than an HTTP error."""

    async def test_reading_a_draft_needs_a_web_session(self):
        with pytest.raises(WebSessionRequired):
            await salary.get_salary_draft(_api(_value(DRAFT)), 7562010)

    async def test_running_needs_a_web_session(self):
        with pytest.raises(WebSessionRequired):
            await salary.run_salary(
                _api(_value(COMMITTED)), 7562010, payment_type_id=-3,
                payment_date=datetime.date(2026, 9, 4),
                payslips_available_date=datetime.date(2026, 9, 4),
            )

    async def test_importing_needs_a_web_session(self):
        with pytest.raises(WebSessionRequired):
            await salary.upload_import_file(_api(_value({})), __file__)


class TestDeleteSalaryDraft:
    """Discarding is what makes an import reversible, and therefore what makes
    testing one against a real company defensible at all."""

    def _client(self, handler):
        client = TripletexClient(TripletexConfig(base_url=BASE_URL))
        client._session = WebSession(cookies=httpx.Cookies(), context_id="1")
        client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BASE_URL
        )
        return client

    def _serving(self, committed: bool, seen: list | None = None):
        def handler(request: httpx.Request) -> httpx.Response:
            if seen is not None:
                seen.append((request.method, request.url.path))
            if request.method == "DELETE":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"value": {
                "id": 7588616,
                "completed": committed,
                "voucher": {"id": 99} if committed else None,
                "displayName": "Salary voucher 14-2026" if committed
                               else "Salary payment (under process)",
            }})

        return handler

    async def test_deletes_a_draft(self):
        seen: list[tuple[str, str]] = []

        await salary.delete_salary_draft(self._client(self._serving(False, seen)), 7588616)

        assert ("DELETE", "/v2/tsk/salaryv2/transaction/7588616") in seen

    async def test_refuses_a_committed_run(self):
        """The same route deletes both, and on a committed run that reverses the
        voucher and reissues payslips for everyone."""
        seen: list[tuple[str, str]] = []

        with pytest.raises(ValueError, match="already committed"):
            await salary.delete_salary_draft(self._client(self._serving(True, seen)), 7588616)

        assert not [s for s in seen if s[0] == "DELETE"]

    async def test_force_deletes_a_committed_run_without_checking(self):
        seen: list[tuple[str, str]] = []

        await salary.delete_salary_draft(
            self._client(self._serving(True, seen)), 7588616, force=True
        )

        assert [s[0] for s in seen] == ["DELETE"]

    async def test_force_is_keyword_only(self):
        """So it cannot be passed by accident as a positional."""
        import inspect

        p = inspect.signature(salary.delete_salary_draft).parameters["force"]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY

    async def test_api_token_is_refused(self):
        client = TripletexClient(TripletexConfig(base_url=BASE_URL))
        client._session = ApiSession(session_token="tok", company_id=0)
        client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(self._serving(False)), base_url=BASE_URL
        )

        with pytest.raises(WebSessionRequired):
            await salary.delete_salary_draft(client, 7588616)

    async def test_api_token_is_refused_even_with_force(self):
        """`force` skips the draft lookup, so this function's own session check
        is the only thing between a token and a DELETE. Without it the request
        goes out and the failure is whatever Tripletex happens to answer."""
        seen: list[tuple[str, str]] = []
        client = TripletexClient(TripletexConfig(base_url=BASE_URL))
        client._session = ApiSession(session_token="tok", company_id=0)
        client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(self._serving(False, seen)), base_url=BASE_URL
        )

        with pytest.raises(WebSessionRequired):
            await salary.delete_salary_draft(client, 7588616, force=True)

        assert not [x for x in seen if x[0] == "DELETE"]
