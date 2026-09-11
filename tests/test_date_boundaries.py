"""Every endpoint that takes a date range must convert the end date.

Tripletex documents `dateTo` as exclusive on every date-ranged path this library
calls; pytripletex's own signatures take `date_to` as inclusive. The conversion
happens in `endpoints._dates.exclusive_end`, and this file is the net that
catches a new endpoint added without it.

The bug this guards against shipped once. On account 1500 over
2026-01-01..06-30, sending the caller's date unconverted returned 333 of 346
postings and reported the movement as -23 286.00 rather than +20 182.00 — a sign
flip, because the lost day was month-end. It reads as a plausible number, not as
an error, which is what makes it worth a dedicated test file.
"""

from __future__ import annotations

import datetime

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints._dates import exclusive_end
from tripletex.endpoints.invoices import list_reminders
from tripletex.endpoints.ledger import (
    list_close_groups,
    list_postings,
    list_vouchers_with_postings,
)
from tripletex.endpoints.vouchers import (
    list_non_posted_vouchers,
    list_reception_vouchers,
    list_vouchers,
)
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

#: A range ending on a month end — the case that loses the most, and the shape
#: an audit query actually takes.
H1 = (datetime.date(2026, 1, 1), datetime.date(2026, 6, 30))
EXPECTED_TO = "2026-07-01"


def _client(seen: list[httpx.URL]) -> TripletexClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"values": [], "fullResultSize": 0})

    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


#: Every public call that sends a date range. A new one belongs here.
RANGED = [
    pytest.param(list_postings, id="ledger.list_postings"),
    pytest.param(list_vouchers_with_postings, id="ledger.list_vouchers_with_postings"),
    pytest.param(list_close_groups, id="ledger.list_close_groups"),
    pytest.param(list_vouchers, id="vouchers.list_vouchers"),
    pytest.param(list_non_posted_vouchers, id="vouchers.list_non_posted_vouchers"),
    pytest.param(list_reception_vouchers, id="vouchers.list_reception_vouchers"),
    pytest.param(list_reminders, id="invoices.list_reminders"),
]


class TestEveryRangedCallConverts:
    @pytest.mark.parametrize("call", RANGED)
    async def test_end_date_goes_out_exclusive(self, call):
        seen: list[httpx.URL] = []

        await call(_client(seen), *H1)

        assert seen, f"{call.__name__} issued no request"
        sent = seen[0].params["dateTo"]
        assert sent == EXPECTED_TO, (
            f"{call.__name__} sent dateTo={sent!r}; Tripletex treats it as "
            f"exclusive, so an inclusive 2026-06-30 must go out as {EXPECTED_TO}"
        )

    @pytest.mark.parametrize("call", RANGED)
    async def test_start_date_is_passed_through(self, call):
        """`dateFrom` is inclusive on both sides — it must not be shifted."""
        seen: list[httpx.URL] = []

        await call(_client(seen), *H1)

        assert seen[0].params["dateFrom"] == "2026-01-01"


class TestExclusiveEnd:
    def test_adds_exactly_one_day(self):
        assert exclusive_end(datetime.date(2026, 6, 30)) == "2026-07-01"

    def test_crosses_a_year_boundary(self):
        assert exclusive_end(datetime.date(2025, 12, 31)) == "2026-01-01"

    def test_handles_a_leap_day(self):
        assert exclusive_end(datetime.date(2028, 2, 28)) == "2028-02-29"

    def test_a_single_day_range_is_not_empty(self):
        """Asking for one day must ask for a real window. Tripletex refuses
        `dateFrom == dateTo` with a 422, so passing a day through unconverted
        does not even return the wrong answer — it fails."""
        day = datetime.date(2026, 6, 5)

        assert exclusive_end(day) != day.isoformat()
        assert exclusive_end(day) == "2026-06-06"
