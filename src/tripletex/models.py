"""Pydantic data models for Tripletex entities."""

import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


# --- Companies ---


class Company(BaseModel):
    id: int
    display_name: str = Field(alias="displayName")
    # Populated by get_company() — not returned by the company chooser endpoint.
    organization_number: Optional[str] = Field(default=None, alias="organizationNumber")

    model_config = {"populate_by_name": True}


# --- Bank / Reconciliation ---


class BankAccount(BaseModel):
    id: int
    number: Optional[int] = Field(default=None, alias="number")
    iban: Optional[str] = Field(default=None, alias="bankAccountIBAN")
    name: Optional[str] = Field(default=None, alias="name")
    require_reconciliation: bool = Field(default=False, alias="requireReconciliation")

    model_config = {"populate_by_name": True}


class AccountingPeriod(BaseModel):
    id: int
    start: datetime.date
    end: datetime.date = Field(alias="end")

    model_config = {"populate_by_name": True}


class BankTransaction(BaseModel):
    id: int
    posted_date: datetime.date = Field(alias="postedDate")
    amount_currency: Decimal = Field(alias="amountCurrency")
    description: str = ""
    details: Optional[str] = None

    model_config = {"populate_by_name": True}


class Reconciliation(BaseModel):
    id: int
    is_closed: bool = Field(default=False, alias="isClosed")
    closing_balance: Optional[Decimal] = Field(
        default=None, alias="bankAccountClosingBalanceCurrency"
    )
    transactions: list[BankTransaction] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# --- Payments ---


class Payment(BaseModel):
    voucher: str
    payment_account: str
    recipient: str
    status: str
    due_date: datetime.date
    amount: str


# --- Vouchers ---


class VoucherMeta(BaseModel):
    id: int
    number: Optional[int] = None
    # Non-posted vouchers have number 0 and are identified by tempNumber, which
    # is what the Tripletex UI shows for them.
    temp_number: Optional[int] = Field(default=None, alias="tempNumber")
    year: Optional[int] = None
    date: Optional[datetime.date] = None
    description: Optional[str] = None
    document_ids: list[int] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def display_number(self) -> str:
        """Voucher number, or the temp number for a not-yet-posted voucher."""
        if self.number:
            return str(self.number)
        return f"T{self.temp_number}" if self.temp_number else ""


# --- Ledger: chart of accounts and postings ---


class VatType(BaseModel):
    """A VAT treatment. `percentage` can be negative — id 34 is -25%, used to
    reverse high-rate input VAT on a credit note."""

    id: int
    name: Optional[str] = None
    percentage: Optional[Decimal] = None

    model_config = {"populate_by_name": True}


class Account(BaseModel):
    """A chart-of-accounts row.

    `vat_type` is the account's *default* treatment, not what any posting used;
    a posting carries its own. Comparing the two is the cheapest classification
    check there is, but a difference is a question, not a defect — see
    `ledger.vat_deviations`.
    """

    # Optional because a nested `account(...)` expansion returns only the fields
    # the caller asked for, and `number` is the useful key anyway — it is the
    # stable chart-of-accounts number, while `id` is internal.
    id: Optional[int] = None
    number: Optional[int] = None
    name: Optional[str] = None
    type: Optional[str] = None
    vat_type: Optional[VatType] = Field(default=None, alias="vatType")
    is_bank_account: bool = Field(default=False, alias="isBankAccount")
    is_inactive: bool = Field(default=False, alias="isInactive")

    model_config = {"populate_by_name": True}


class Posting(BaseModel):
    """One line of a voucher.

    `amount` is signed and in NOK; `amount_currency` is the same figure in
    `currency` when the voucher was raised in a foreign one. Both arrive as
    NOK-equal on domestic vouchers.
    """

    id: int
    date: Optional[datetime.date] = None
    description: Optional[str] = None
    amount: Optional[Decimal] = None
    amount_currency: Optional[Decimal] = Field(default=None, alias="amountCurrency")
    currency: Optional[dict] = None
    account: Optional[Account] = None
    vat_type: Optional[VatType] = Field(default=None, alias="vatType")
    supplier: Optional[dict] = None
    customer: Optional[dict] = None
    employee: Optional[dict] = None
    department: Optional[dict] = None
    project: Optional[dict] = None
    row: Optional[int] = None

    model_config = {"populate_by_name": True}


class LedgerVoucher(BaseModel):
    """A voucher with its postings and receipt expanded.

    `voucher_type` is `None` on manually entered vouchers and on those written
    by integrations — 72 of 326 in one measured month — so it cannot be used as
    a required key. `attachment` is `None` when the voucher carries no document,
    which is correct and expected for payment runs and salary vouchers.
    """

    id: int
    number: Optional[int] = None
    temp_number: Optional[int] = Field(default=None, alias="tempNumber")
    year: Optional[int] = None
    date: Optional[datetime.date] = None
    description: Optional[str] = None
    voucher_type: Optional[dict] = Field(default=None, alias="voucherType")
    attachment: Optional[dict] = None
    postings: list[Posting] = Field(default_factory=list)
    changes: list[dict] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def has_attachment(self) -> bool:
        return bool(self.attachment and self.attachment.get("id"))

    @property
    def voucher_type_name(self) -> Optional[str]:
        return (self.voucher_type or {}).get("name")


# --- Invoice reminders ---


class Reminder(BaseModel):
    """A chase sent on an unpaid customer invoice.

    `type` matters more than it looks. `SOFT_REMINDER` is a courtesy nudge and
    is not supposed to carry a fee; `REMINDER` is the formal purring that may.
    Measured on Bonita Services 2024-2026: 32 soft against 7 formal, and only 4
    of the 39 carried any charge or interest at all. Counting them together
    makes a company look as though it never charges when mostly it never
    escalated.
    """

    id: int
    invoice_id: Optional[int] = Field(default=None, alias="invoiceId")
    reminder_date: Optional[datetime.date] = Field(default=None, alias="reminderDate")
    type: Optional[str] = None
    charge: Optional[Decimal] = None
    total_charge: Optional[Decimal] = Field(default=None, alias="totalCharge")
    interests: Optional[Decimal] = None
    interest_rate: Optional[Decimal] = Field(default=None, alias="interestRate")
    total_amount_currency: Optional[Decimal] = Field(
        default=None, alias="totalAmountCurrency"
    )
    term_of_payment: Optional[datetime.date] = Field(
        default=None, alias="termOfPayment"
    )
    comment: Optional[str] = None

    model_config = {"populate_by_name": True}

    @property
    def is_formal(self) -> bool:
        """A formal purring rather than a courtesy nudge."""
        return (self.type or "").upper() == "REMINDER"

    @property
    def cost_to_customer(self) -> Decimal:
        return (self.total_charge or Decimal(0)) + (self.interests or Decimal(0))


# --- Employment and wage history ---


class SalaryRow(BaseModel):
    """One salary as of a date — an `EmploymentDetails` row.

    An employment carries a list of these, which together are the wage history:
    each row states what the terms became on `date` and holds until the next.
    Bonita Handel has 180 of them across 81 employments, reaching back to 2015.
    """

    id: Optional[int] = None
    date: Optional[datetime.date] = None
    annual_salary: Optional[Decimal] = Field(default=None, alias="annualSalary")
    monthly_salary: Optional[Decimal] = Field(default=None, alias="monthlySalary")
    hourly_wage: Optional[Decimal] = Field(default=None, alias="hourlyWage")
    percentage_of_full_time: Optional[Decimal] = Field(
        default=None, alias="percentageOfFullTimeEquivalent"
    )
    employment_type: Optional[str] = Field(default=None, alias="employmentType")
    employment_form: Optional[str] = Field(default=None, alias="employmentForm")
    remuneration_type: Optional[str] = Field(default=None, alias="remunerationType")
    working_hours_scheme: Optional[str] = Field(
        default=None, alias="workingHoursScheme"
    )
    shift_duration_hours: Optional[Decimal] = Field(
        default=None, alias="shiftDurationHours"
    )
    occupation_code: Optional[dict] = Field(default=None, alias="occupationCode")

    model_config = {"populate_by_name": True}


class LeaveOfAbsence(BaseModel):
    """A period of leave against an employment."""

    id: int
    employment: Optional[dict] = None
    start_date: Optional[datetime.date] = Field(default=None, alias="startDate")
    end_date: Optional[datetime.date] = Field(default=None, alias="endDate")
    percentage: Optional[Decimal] = None
    type: Optional[str] = None
    is_wage_deduction: bool = Field(default=False, alias="isWageDeduction")

    model_config = {"populate_by_name": True}


class HolidaySettings(BaseModel):
    """Vacation entitlement and pay percentages for a year.

    `vacation_pay_percentage_2` is the over-60 rate, which the HTML scrape this
    replaces never picked up.
    """

    id: int
    year: Optional[int] = None
    days: Optional[Decimal] = None
    vacation_pay_percentage: Optional[Decimal] = Field(
        default=None, alias="vacationPayPercentage1"
    )
    vacation_pay_percentage_2: Optional[Decimal] = Field(
        default=None, alias="vacationPayPercentage2"
    )
    is_max_percentage_2_amount_6g: bool = Field(
        default=False, alias="isMaxPercentage2Amount6G"
    )

    model_config = {"populate_by_name": True}


# --- Tax cards (skattekort) ---


class AdvanceTaxcard(BaseModel):
    """One deduction rule on a tax card — a *trekkode*.

    A card carries several, one per income kind (main employer, secondary
    employer, NAV), and they can differ. `type` 2 is a percentage card
    (Prosentkort), where `prosentsats` applies; a table card uses `tabellnummer`
    instead. `frikortbelop` is the tax-free allowance on a Frikort.
    """

    trekkode: Optional[str] = None
    trekkode_description: Optional[str] = Field(default=None, alias="trekkodeDescription")
    type: Optional[int] = None
    type_description: Optional[str] = Field(default=None, alias="typeDescription")
    tabelltype: Optional[str] = None
    tabellnummer: Optional[str] = None
    prosentsats: Optional[Decimal] = None
    antall_mnd_for_trekk: Optional[Decimal] = Field(default=None, alias="antallMndForTrekk")
    frikortbelop: Optional[Decimal] = None
    remaining_free_card_amount: Optional[Decimal] = Field(
        default=None, alias="remainingFreeCardAmount"
    )

    model_config = {"populate_by_name": True}


#: The one status meaning nothing is wrong. Everything else needs a human.
TAXCARD_OK = "skattekortopplysningerOK"


class Taxcard(BaseModel):
    """An employee's tax card for a year, as Tripletex holds it.

    **Decide OK-ness from `status`, never from `status_description`.** The
    description is accurate for the failure statuses but wrong for the healthy
    one: `skattekortopplysningerOK` is described as *"det har oppstått en ukjent
    feil"* — "an unknown error occurred" — on every good card, across 2024-2026
    on Bonita Handel (35, 40 and 42 cards). It reads like a missing enum entry
    falling through to a generic error string.

    Confirmed not to be a request-side mistake, which was the obvious
    suspicion: the same pair comes back from `taxcard(*)`, from an explicit
    `taxcard(status,statusDescription)`, from `statusDescription` alone, from
    fetching one card directly by id, and from the Tripletex UI's own request
    captured in the browser.

    So the field is fine to *show* once a card is known to be in a failure
    state — it is the only human-readable text available, and it is correct
    there. It just cannot be used to determine whether that state exists.
    """

    id: Optional[int] = None
    status: Optional[str] = None
    status_description: Optional[str] = Field(default=None, alias="statusDescription")
    #: Free text alongside the status. `kildeskattPaaLoenn` — PAYE for foreign
    #: workers — appears here while `status` stays OK, so it is a note rather
    #: than a fault, but one payroll needs to know about.
    additional_info: Optional[str] = Field(default=None, alias="additionalInfo")
    year_of_income: Optional[int] = Field(default=None, alias="yearOfIncome")
    date: Optional[datetime.date] = None
    issued_date: Optional[datetime.date] = Field(default=None, alias="utstedtDato")
    employee_identifier: Optional[str] = Field(
        default=None, alias="arbeidstakerIdentifikator"
    )
    order_id: Optional[int] = Field(default=None, alias="orderId")
    deduction_period: Optional[int] = Field(default=None, alias="deductionPeriod")
    advance_taxcards: list[AdvanceTaxcard] = Field(
        default_factory=list, alias="advanceTaxcards"
    )

    model_config = {"populate_by_name": True}

    @property
    def is_ok(self) -> bool:
        return self.status == TAXCARD_OK


class TaxcardEmployee(BaseModel):
    """An employee and their tax card, or the absence of one.

    `taxcard` is `None` when no card was ever ordered or returned — distinct
    from a card that came back with a problem, and worth separating: one is a
    process failure, the other a fact about the person.
    """

    id: Optional[int] = None
    display_name: Optional[str] = Field(default=None, alias="displayName")
    number: Optional[str] = None
    taxcard: Optional[Taxcard] = None

    model_config = {"populate_by_name": True}

    @property
    def has_card(self) -> bool:
        return self.taxcard is not None and self.taxcard.id is not None

    @property
    def issue(self) -> Optional[str]:
        """What needs attention, or None. See `Taxcard` for why not the
        description field."""
        if not self.has_card:
            return "no tax card has been ordered or returned"
        if not self.taxcard.is_ok:
            return self.taxcard.status
        return None

    @property
    def note(self) -> Optional[str]:
        """A flag that is not a fault — `kildeskattPaaLoenn` and the like."""
        info = (self.taxcard.additional_info or "").strip() if self.taxcard else ""
        return info or None


# --- Dashboard compliance reminders ---


class DashboardReminder(BaseModel):
    """A statutory deadline Tripletex shows on its dashboard.

    Nothing to do with `Reminder`, which chases a customer for money. These are
    filing and payment obligations — Skattemelding, A-melding, AGA — and the
    colour is Tripletex's own urgency verdict, so it is worth carrying rather
    than recomputing from `remaining_days`.

    Flattened from a three-level response (`globalReminder`, `companyReminder`,
    `reminderBorderColor`) because the nesting carries no information a caller
    wants: the deadline and the company's progress against it are one fact.
    """

    id: int
    name: Optional[str] = None
    display_name: Optional[str] = Field(default=None, alias="displayName")
    deadline: Optional[datetime.date] = None
    remaining_days: Optional[int] = Field(default=None, alias="remainingDays")
    term: Optional[str] = None
    url: Optional[str] = Field(default=None, alias="reminderUrl")
    #: `None` when the company has not started — no companyReminder row exists.
    status: Optional[str] = None
    submission_date: Optional[datetime.date] = Field(
        default=None, alias="submissionDate"
    )
    border_color: Optional[str] = Field(default=None, alias="reminderBorderColor")

    model_config = {"populate_by_name": True}

    @property
    def is_urgent(self) -> bool:
        """Tripletex's own verdict: red or yellow."""
        return (self.border_color or "").upper() in ("RED", "YELLOW")

    @property
    def is_overdue(self) -> bool:
        return self.remaining_days is not None and self.remaining_days < 0

    @property
    def is_done(self) -> bool:
        return (self.status or "").upper() == "COMPLETED"


# --- Travel expenses ---


class TravelExpense(BaseModel):
    """An expense claim (reiseregning / utleggsrefusjon).

    `state` is the workflow position; `DELIVERED` means submitted and waiting
    for someone to approve it, which is the queue worth watching.
    """

    id: int
    number: Optional[int] = None
    title: Optional[str] = None
    # When the expense was incurred — not when it entered the approval queue.
    date: Optional[datetime.date] = None
    # When the employee submitted it. This is what ages a pending claim.
    completed_date: Optional[datetime.date] = Field(
        default=None, alias="completedDate"
    )
    approved_date: Optional[datetime.date] = Field(default=None, alias="approvedDate")
    amount: Optional[Decimal] = None
    payment_amount: Optional[Decimal] = Field(default=None, alias="paymentAmount")
    state: Optional[str] = None
    # Localised, so it follows the account language — "Levert" for DELIVERED on
    # a Norwegian account. Display only; branch on `state`.
    state_name: Optional[str] = Field(default=None, alias="stateName")
    is_approved: bool = Field(default=False, alias="isApproved")
    is_completed: bool = Field(default=False, alias="isCompleted")
    employee: Optional[dict] = None
    approved_by: Optional[dict] = Field(default=None, alias="approvedBy")
    department: Optional[dict] = None
    project: Optional[dict] = None
    voucher: Optional[dict] = None
    attachment_count: int = Field(default=0, alias="attachmentCount")
    rejected_comment: Optional[str] = Field(default=None, alias="rejectedComment")

    model_config = {"populate_by_name": True}

    @property
    def employee_name(self) -> str:
        e = self.employee or {}
        return " ".join(
            p for p in (e.get("firstName"), e.get("lastName")) if p
        ).strip()

    @property
    def waiting_days(self) -> Optional[int]:
        """Days since the claim was submitted for approval.

        Ages from `completed_date`, falling back to the expense `date` when a
        claim carries no submission date — the two coincide on same-day claims,
        and `date` is never later, so this never under-reports the wait.
        """
        since = self.completed_date or self.date
        if since is None:
            return None
        return (datetime.date.today() - since).days


# --- Document reception ---


class DocumentReceptionItem(BaseModel):
    """A file waiting in the Documents reception, addressed to an employee.

    A different queue from voucher reception (bilagsmottak): these are files sent
    to `<employeeId>.inbox@arkiv.tripletex.no`, carrying no voucher, supplier or
    amount. See `adapter-notes.md` for what is and is not usable here.
    """

    document_id: int = Field(alias="documentId")
    message_id: Optional[int] = Field(default=None, alias="messageId")
    document_name: Optional[str] = Field(default=None, alias="documentName")
    mime_type: Optional[str] = Field(default=None, alias="mimeType")
    size: Optional[int] = None
    display_size: Optional[str] = Field(default=None, alias="displaySize")
    # Whose queue this sits in — the one triage axis the queue actually carries.
    receiver_employee_id: Optional[int] = Field(
        default=None, alias="receiverEmployeeId"
    )
    receiver_name: Optional[str] = Field(default=None, alias="receiverName")
    # Measured always blank, for uploads and for mailed-in documents alike.
    # Kept because it is in the model Tripletex publishes; do not rely on it.
    sender_name: Optional[str] = Field(default=None, alias="senderName")
    # A bare date, not a timestamp — good enough to age a queue in days, not
    # within one.
    created: Optional[datetime.date] = None
    edited: Optional[datetime.date] = None
    # Measured false on arrival for both a mailed and an uploaded document, so
    # this does not identify unseen items.
    is_new: bool = Field(default=False, alias="isNew")

    model_config = {"populate_by_name": True}

    @property
    def age_days(self) -> Optional[int]:
        """Whole days since the document arrived, or None if undated."""
        if self.created is None:
            return None
        return (datetime.date.today() - self.created).days


class DocumentReceptionContext(BaseModel):
    """Where documents come in, and what the caller may see."""

    document_reception_email: Optional[str] = Field(
        default=None, alias="documentReceptionEmail"
    )
    max_file_size: Optional[int] = Field(default=None, alias="maxFileSize")
    # True when the caller sees every employee's documents rather than only
    # their own — the difference between "the queue is empty" and "my queue is".
    auth_all_employees: bool = Field(default=False, alias="authAllEmployees")
    auth_voucher_reception: bool = Field(
        default=False, alias="authVoucherReception"
    )

    model_config = {"populate_by_name": True}


# --- Wages / Employees ---


class SalaryEntry(BaseModel):
    date: Optional[datetime.date] = None
    yearly_wages: Optional[Decimal] = None
    hourly_wage: Optional[Decimal] = None
    percent_of_employment: Optional[Decimal] = None


class Employment(BaseModel):
    index: int = 0
    start_date: Optional[datetime.date] = None
    division: Optional[str] = None
    salaries: list[SalaryEntry] = Field(default_factory=list)


class EmployeeSalary(BaseModel):
    employee_number: Optional[str] = None
    employments: list[Employment] = Field(default_factory=list)
    feriepenger_rate: Optional[Decimal] = None


class CompanyWageSettings(BaseModel):
    feriepenger_rate_1: Optional[Decimal] = None
    feriepenger_rate_2: Optional[Decimal] = None
    vacation_days: Optional[int] = None


# --- Employees (API) ---


class EmploymentPeriod(BaseModel):
    """One employment period (the API's Employment object).

    Distinct from `Employment` above, which is the salary-history block scraped
    from the employee salary page. Both exist because the scrape predates the
    discovery that `/v2/employee/employment` carries the same history; new work
    should use this one.
    """

    id: Optional[int] = None
    employee: Optional[dict] = None
    employment_id: Optional[str] = Field(default=None, alias="employmentId")
    start_date: Optional[datetime.date] = Field(default=None, alias="startDate")
    end_date: Optional[datetime.date] = Field(default=None, alias="endDate")
    end_reason: Optional[str] = Field(default=None, alias="employmentEndReason")
    division: Optional[dict] = None
    is_main_employer: bool = Field(default=False, alias="isMainEmployer")
    tax_deduction_code: Optional[str] = Field(default=None, alias="taxDeductionCode")
    last_salary_change_date: Optional[datetime.date] = Field(
        default=None, alias="lastSalaryChangeDate"
    )
    #: The wage history. Each row states what the terms became on its date and
    #: holds until the next supersedes it. Empty unless asked for — see
    #: `endpoints.employees.list_employments`.
    salary_history: list["SalaryRow"] = Field(
        default_factory=list, alias="employmentDetails"
    )
    # When set, Tripletex revokes the employee's login when the period ends.
    removes_access_at_end: bool = Field(
        default=False, alias="isRemoveAccessAtEmploymentEnded"
    )

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def division_name(self) -> str:
        """Division (unit) name, if the nested division object was expanded."""
        return self.division.get("name", "") if self.division else ""

    @property
    def division_organization_number(self) -> Optional[str]:
        """The unit's org number as a field, rather than scraped out of a name."""
        return (self.division or {}).get("organizationNumber")

    def salary_on(self, when: datetime.date) -> Optional["SalaryRow"]:
        """The row in force on `when` — the latest dated at or before it."""
        applicable = [
            r for r in self.salary_history if r.date is not None and r.date <= when
        ]
        return max(applicable, key=lambda r: r.date) if applicable else None

    def is_active(self, on: Optional[datetime.date] = None) -> bool:
        """True if the period covers `on` (today by default)."""
        on = on or datetime.date.today()
        if self.start_date and self.start_date > on:
            return False
        return self.end_date is None or self.end_date >= on


class Employee(BaseModel):
    id: Optional[int] = None
    first_name: Optional[str] = Field(default=None, alias="firstName")
    last_name: Optional[str] = Field(default=None, alias="lastName")
    display_name: str = Field(default="", alias="displayName")
    employee_number: Optional[str] = Field(default=None, alias="employeeNumber")
    email: Optional[str] = None
    department: Optional[dict] = None
    employments: list[EmploymentPeriod] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def department_name(self) -> str:
        """Department name, if the nested department object was expanded."""
        return self.department.get("name", "") if self.department else ""

    def active_employments(
        self, on: Optional[datetime.date] = None
    ) -> list[EmploymentPeriod]:
        """Employment periods covering `on` (today by default)."""
        return [e for e in self.employments if e.is_active(on)]

    def has_active_employment(self, on: Optional[datetime.date] = None) -> bool:
        return bool(self.active_employments(on))

    @property
    def latest_employment(self) -> Optional[EmploymentPeriod]:
        """Employment period with the most recent start date."""
        dated = [e for e in self.employments if e.start_date]
        if not dated:
            return self.employments[0] if self.employments else None
        return max(dated, key=lambda e: e.start_date)


class EmployeeOverview(BaseModel):
    """A row from the internal salary employee overview.

    `payslip_delivery` is a display string localized to the logged-in user's
    language — "The Tripletex app" / "Manual handling" in English, "Tripletex-appen"
    / "Manuell håndtering" in Norwegian.
    """

    id: Optional[int] = None
    display_name: str = Field(default="", alias="displayName")
    employee_number: Optional[str] = Field(default=None, alias="number")
    payslip_delivery: Optional[str] = Field(
        default=None, alias="deliveryMethodWageSlipString"
    )
    allow_login: bool = Field(default=False, alias="allowLogin")
    has_resigned: bool = Field(default=False, alias="hasResigned")

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def payslip_via_app(self) -> bool:
        """True if payslips go through the Tripletex app rather than manually.

        Matched on the word "app", which holds for both the English ("The Tripletex
        app") and Norwegian ("Tripletex-appen") strings; the manual variants
        ("Manual handling" / "Manuell håndtering") contain no "app".
        """
        return "app" in (self.payslip_delivery or "").lower()


class EmployeeAccess(BaseModel):
    """Login access settings from an employee's "User access" tab.

    `allow_login` is the account toggle; `login_end_date` is the last day the
    login works. Ending an employment that has `removes_access_at_end` set makes
    Tripletex fill in `login_end_date` (or clear `allow_login`) — and it does not
    undo that when a later employment starts.
    """

    employee_id: int
    allow_login: bool = False
    login_end_date: Optional[datetime.date] = None
    reg_info_end_date: Optional[datetime.date] = None

    def access_ended(self, on: Optional[datetime.date] = None) -> bool:
        """True if the employee cannot log in as of `on` (today by default)."""
        on = on or datetime.date.today()
        if not self.allow_login:
            return True
        return self.login_end_date is not None and self.login_end_date < on


# --- Customers (API) ---


class Customer(BaseModel):
    id: Optional[int] = None
    name: str = ""
    organization_number: Optional[str] = Field(default=None, alias="organizationNumber")
    email: Optional[str] = None
    phone_number: Optional[str] = Field(default=None, alias="phoneNumber")
    is_customer: bool = Field(default=True, alias="isCustomer")
    is_supplier: bool = Field(default=False, alias="isSupplier")
    customer_number: Optional[int] = Field(default=None, alias="customerNumber")

    model_config = {"populate_by_name": True, "extra": "allow"}


# --- Products (API) ---


class Product(BaseModel):
    id: Optional[int] = None
    name: str = ""
    number: Optional[str] = None
    cost_excluding_vat_currency: Optional[Decimal] = Field(
        default=None, alias="costExcludingVatCurrency"
    )
    price_excluding_vat_currency: Optional[Decimal] = Field(
        default=None, alias="priceExcludingVatCurrency"
    )
    price_including_vat_currency: Optional[Decimal] = Field(
        default=None, alias="priceIncludingVatCurrency"
    )
    is_inactive: bool = Field(default=False, alias="isInactive")

    model_config = {"populate_by_name": True, "extra": "allow"}


# --- Orders (API) ---


class OrderLine(BaseModel):
    id: Optional[int] = None
    order: Optional[dict] = None
    product: Optional[dict] = None
    description: Optional[str] = None
    count: Optional[Decimal] = None
    unit_cost_currency: Optional[Decimal] = Field(default=None, alias="unitCostCurrency")
    unit_price_excluding_vat_currency: Optional[Decimal] = Field(
        default=None, alias="unitPriceExcludingVatCurrency"
    )
    amount_excluding_vat_currency: Optional[Decimal] = Field(
        default=None, alias="amountExcludingVatCurrency"
    )
    amount_including_vat_currency: Optional[Decimal] = Field(
        default=None, alias="amountIncludingVatCurrency"
    )

    model_config = {"populate_by_name": True, "extra": "allow"}


class Order(BaseModel):
    id: Optional[int] = None
    number: Optional[str] = None
    reference: Optional[str] = None
    customer: Optional[dict] = None
    order_date: Optional[datetime.date] = Field(default=None, alias="orderDate")
    delivery_date: Optional[datetime.date] = Field(default=None, alias="deliveryDate")
    receiver_email: Optional[str] = Field(default=None, alias="receiverEmail")
    order_lines: Optional[list[OrderLine]] = Field(default=None, alias="orderLines")
    is_closed: bool = Field(default=False, alias="isClosed")

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def customer_name(self) -> str:
        """Customer name, if the nested customer object was expanded."""
        if self.customer:
            return self.customer.get("name") or self.customer.get("displayName") or ""
        return ""

    @property
    def amount_including_vat(self) -> Optional[Decimal]:
        """Order total incl. VAT, summed from order lines (None if unavailable)."""
        return self._sum_lines("amount_including_vat_currency")

    @property
    def amount_excluding_vat(self) -> Optional[Decimal]:
        """Order total excl. VAT, summed from order lines (None if unavailable)."""
        return self._sum_lines("amount_excluding_vat_currency")

    def _sum_lines(self, attr: str) -> Optional[Decimal]:
        if self.order_lines is None:
            return None
        return sum(
            (getattr(line, attr) or Decimal(0) for line in self.order_lines),
            Decimal(0),
        )


# --- Invoices (API) ---


class Invoice(BaseModel):
    id: Optional[int] = None
    invoice_number: Optional[int] = Field(default=None, alias="invoiceNumber")
    order: Optional[dict] = None
    orders: Optional[list[dict]] = None
    customer: Optional[dict] = None
    currency: Optional[dict] = None
    invoice_date: Optional[datetime.date] = Field(default=None, alias="invoiceDate")
    due_date: Optional[datetime.date] = Field(default=None, alias="invoiceDueDate")
    amount: Optional[Decimal] = None
    amount_currency: Optional[Decimal] = Field(default=None, alias="amountCurrency")
    amount_excluding_vat: Optional[Decimal] = Field(
        default=None, alias="amountExcludingVatCurrency"
    )
    amount_outstanding: Optional[Decimal] = Field(
        default=None, alias="amountCurrencyOutstanding"
    )
    is_credit_note: bool = Field(default=False, alias="isCreditNote")
    is_credited: bool = Field(default=False, alias="isCredited")

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def customer_name(self) -> str:
        """Customer name, if the nested customer object was expanded."""
        if self.customer:
            return self.customer.get("name") or self.customer.get("displayName") or ""
        return ""

    @property
    def currency_code(self) -> str:
        """Currency code (e.g. NOK), if the currency object was expanded."""
        return self.currency.get("code", "") if self.currency else ""

    @property
    def reference(self) -> str:
        """Reference, taken from the first linked order that carries one."""
        for o in self.orders or []:
            ref = o.get("reference")
            if ref:
                return ref
        return ""

    @property
    def status(self) -> str:
        """Best single status label, highest priority first."""
        if self.is_credited:
            return "credited"
        if self.is_credit_note:
            return "credit note"
        outstanding = self.amount_outstanding or Decimal(0)
        if outstanding != 0:
            if self.due_date and self.due_date < datetime.date.today():
                return "overdue"
            return "outstanding"
        return "paid"
