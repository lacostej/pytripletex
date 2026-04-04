"""Payment endpoints using the v2/bank/payment JSON API."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


_PAYMENT_FIELDS = (
    "*,"
    "account(id,number,bankAccountIBAN,bankAccountNumber,currency(id,code)),"
    "sourceVoucher(id,wasAutoMatched,number,tempNumber,year,vendorInvoiceNumber),"
    "acceptors(id,displayName),"
    "numberOfApprovedInBank,"
    "currency(id,code),"
    "bank(id,platform)"
)


class BankPayment(BaseModel):
    """A bank payment from /v2/bank/payment."""
    id: int
    payment_date: Optional[datetime.date] = Field(default=None, alias="paymentDate")
    amount_currency: Optional[Decimal] = Field(default=None, alias="amountCurrency")
    status: Optional[str] = None
    kid: Optional[str] = None
    receiver_reference: Optional[str] = Field(default=None, alias="receiverReference")
    source_voucher: Optional[dict] = Field(default=None, alias="sourceVoucher")
    account: Optional[dict] = None
    acceptors: Optional[list[dict]] = None
    currency: Optional[dict] = None

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def voucher_number(self) -> str:
        if self.source_voucher:
            num = self.source_voucher.get("number") or self.source_voucher.get("tempNumber")
            year = self.source_voucher.get("year", "")
            return f"{num}" if num else ""
        return ""

    @property
    def account_number(self) -> str:
        if self.account:
            return (
                self.account.get("bankAccountIBAN")
                or self.account.get("bankAccountNumber")
                or str(self.account.get("number", ""))
            )
        return ""


async def list_payments(
    client: TripletexClient,
    status_filter: str = "FOR_APPROVAL",
    count: int = 10000,
) -> list[BankPayment]:
    """List bank payments.

    GET /v2/bank/payment with JSON response.

    Args:
        client: Authenticated TripletexClient
        status_filter: "FOR_APPROVAL", "APPROVED", "SENT_TO_BANK", "RECEIVED_BY_BANK", etc.
        count: Max results to fetch
    """
    params = {
        "fields": _PAYMENT_FIELDS,
        "sortField": "paymentDate",
        "sortOrder": "ASC",
        "statusFilter": status_filter,
        "paymentCategory": "",
        "includeNonAttested": "true",
        "autoPosted": "false",
        "paymentSource": "AutoPayTransaction",
        "query": "",
        "count": str(count),
        "from": "0",
    }

    data = await client.get_json("/v2/bank/payment", params=params)
    return [BankPayment.model_validate(v) for v in data.get("values", [])]
