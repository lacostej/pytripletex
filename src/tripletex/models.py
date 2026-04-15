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

    model_config = {"populate_by_name": True, "extra": "allow"}


class Order(BaseModel):
    id: Optional[int] = None
    number: Optional[str] = None
    customer: Optional[dict] = None
    order_date: Optional[datetime.date] = Field(default=None, alias="orderDate")
    delivery_date: Optional[datetime.date] = Field(default=None, alias="deliveryDate")
    receiver_email: Optional[str] = Field(default=None, alias="receiverEmail")
    order_lines: Optional[list[OrderLine]] = Field(default=None, alias="orderLines")
    is_closed: bool = Field(default=False, alias="isClosed")

    model_config = {"populate_by_name": True, "extra": "allow"}


# --- Invoices (API) ---


class Invoice(BaseModel):
    id: Optional[int] = None
    invoice_number: Optional[int] = Field(default=None, alias="invoiceNumber")
    order: Optional[dict] = None
    customer: Optional[dict] = None
    invoice_date: Optional[datetime.date] = Field(default=None, alias="invoiceDate")
    due_date: Optional[datetime.date] = Field(default=None, alias="dueDate")
    amount: Optional[Decimal] = None
    amount_currency: Optional[Decimal] = Field(default=None, alias="amountCurrency")
    is_credit_note: bool = Field(default=False, alias="isCreditNote")

    model_config = {"populate_by_name": True, "extra": "allow"}
