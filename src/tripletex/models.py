"""Pydantic data models for Tripletex entities."""

import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


# --- Companies ---


class Company(BaseModel):
    id: int
    display_name: str = Field(alias="displayName")

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
    year: Optional[int] = None
    date: Optional[datetime.date] = None
    description: Optional[str] = None
    document_ids: list[int] = Field(default_factory=list)


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
