"""Payment endpoints using the v2/bank/payment JSON API."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

from tripletex.endpoints._paging import paginate
from tripletex.session import require_web_session

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


# Values `statusFilter` accepts, established by probing: anything else is rejected
# with a 422 whose message ("Listen med verdier må være kommaseparert") does not
# enumerate the alternatives. APPROVED and SENT_TO_BANK are NOT valid, despite
# looking plausible.
PAYMENT_STATUSES = (
    "FOR_APPROVAL",
    "UNDER_PROCESSING",
    "CANCELLED",
    "REJECTED_BY_THE_BANK",
    "PAID",
)

# Everything still in flight — i.e. every status except PAID.
OPEN_PAYMENT_STATUSES = (
    "CANCELLED",
    "REJECTED_BY_THE_BANK",
    "FOR_APPROVAL",
    "UNDER_PROCESSING",
)


def validate_status_filter(status_filter: str) -> str:
    """Check a comma-separated `statusFilter` before spending a request on it."""
    unknown = [
        s for s in (x.strip() for x in status_filter.split(","))
        if s and s not in PAYMENT_STATUSES
    ]
    if unknown:
        raise ValueError(
            f"Unknown payment status {', '.join(unknown)}. "
            f"Valid values: {', '.join(PAYMENT_STATUSES)}"
        )
    return status_filter


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
    limit: int | None = None,
) -> list[BankPayment]:
    """List bank payments.

    GET /v2/bank/payment with JSON response.

    Args:
        client: Authenticated TripletexClient
        status_filter: One of `PAYMENT_STATUSES`, or a comma-separated list of
            them. Note that `APPROVED` and `SENT_TO_BANK` look plausible but are
            rejected with a 422 — see the comment on `PAYMENT_STATUSES`.
        limit: Max results to fetch (default: every match)
    """
    require_web_session(client.session, "Listing bank payments")
    validate_status_filter(status_filter)

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
    }

    values = await paginate(client, "/v2/bank/payment", params=params, limit=limit)
    return [BankPayment.model_validate(v) for v in values]
