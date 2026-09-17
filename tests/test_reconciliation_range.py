"""`get_unreconciled_transactions` must answer for the whole range it is given.

Reconciliation is per accounting period, and this function used to resolve the
range to periods and then use `periods[0]` — answering for the first month and
silently dropping the rest.

The symptom a consumer reported is the signature of this bug class: **widening
the window returned fewer rows**, 17 over one month against 6 over three,
because the first period of the wider range happened to be quieter. Nothing
errored; the number was simply an answer to a different question.
"""

from __future__ import annotations

import datetime

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.reconciliation import get_unreconciled_transactions
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

#: Three months, three periods — what a --months 3 window resolves to.
PERIODS = [
    {"id": 701, "start": "2026-07-01", "end": "2026-08-01"},
    {"id": 702, "start": "2026-08-01", "end": "2026-09-01"},
    {"id": 703, "start": "2026-09-01", "end": "2026-10-01"},
]

ACCOUNT = {"id": 9001, "number": 1920, "name": "Bankinnskudd",
           "requireReconciliation": True, "bankAccountIBAN": None}

#: Transactions per period, deliberately uneven: the first period is the
#: quietest, which is what made the old behaviour look like a smaller answer to
#: a wider question.
BY_PERIOD = {
    701: [1, 2],
    702: [3, 4, 5, 6],
    703: [7, 8, 9, 10, 11, 12],
}


def _txn(i: int) -> dict:
    return {"id": i, "postedDate": f"2026-07-{(i % 28) + 1:02d}",
            "amountCurrency": 100 + i, "description": f"txn {i}"}


def _client(seen: list | None = None, approved: set[int] | None = None):
    approved = approved or set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append((path, dict(request.url.params)))

        if path == "/v2/ledger/account":
            return httpx.Response(200, json={"values": [ACCOUNT], "fullResultSize": 1})
        if path == "/v2/ledger/accountingPeriod":
            return httpx.Response(200, json={"values": PERIODS, "fullResultSize": 3})
        if path == "/v2/bank/reconciliation/match":
            return httpx.Response(200, json={"values": [
                {"id": 1, "transactions": [{"id": i} for i in sorted(approved)],
                 "approved": True}], "fullResultSize": 1})
        if path == "/v2/bank/reconciliation":
            pid = int(request.url.params["accountingPeriodId"])
            txns = [_txn(i) for i in BY_PERIOD.get(pid, [])]
            if not txns:
                return httpx.Response(200, json={"values": [], "fullResultSize": 0})
            return httpx.Response(200, json={"values": [{
                "id": 8000 + pid, "isClosed": False,
                "bankAccountClosingBalanceCurrency": 0,
                "transactions": txns}], "fullResultSize": 1})
        return httpx.Response(200, json={"values": [], "fullResultSize": 0})

    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


JUL = datetime.date(2026, 7, 1)
SEP_END = datetime.date(2026, 9, 30)


class TestSweepsEveryPeriod:
    async def test_all_three_periods_are_queried(self):
        seen: list[tuple[str, dict]] = []

        await get_unreconciled_transactions(_client(seen), JUL, SEP_END)

        asked = {p["accountingPeriodId"] for path, p in seen
                 if path == "/v2/bank/reconciliation"}
        assert asked == {"701", "702", "703"}

    async def test_transactions_from_every_period_are_returned(self):
        ((_, txns),) = await get_unreconciled_transactions(_client(), JUL, SEP_END)

        assert [t.id for t in txns] == list(range(1, 13))

    async def test_a_wider_window_never_returns_fewer_rows(self):
        """The reported symptom, as a property. One month must not beat three."""
        one = await get_unreconciled_transactions(
            _client(), JUL, datetime.date(2026, 7, 31)
        )
        three = await get_unreconciled_transactions(_client(), JUL, SEP_END)

        widest = sum(len(t) for _, t in three)
        narrow = sum(len(t) for _, t in one)
        assert widest >= narrow, f"three months gave {widest}, one month {narrow}"

    async def test_approved_matches_are_excluded_in_every_period(self):
        """Not just in the first one — the exclusion has to travel with the
        sweep, or later periods come back over-reported."""
        client = _client(approved={3, 4, 11})

        ((_, txns),) = await get_unreconciled_transactions(client, JUL, SEP_END)

        assert [t.id for t in txns] == [1, 2, 5, 6, 7, 8, 9, 10, 12]

    async def test_results_are_ordered_by_posting_date(self):
        ((_, txns),) = await get_unreconciled_transactions(_client(), JUL, SEP_END)

        dates = [t.posted_date for t in txns]
        assert dates == sorted(dates)

    async def test_a_transaction_seen_twice_is_counted_once(self):
        """Periods should not overlap, but a duplicate must not double-count if
        they ever do — a count is the thing a monitor alerts on."""
        client = _client()
        # Same transaction id served by two periods.
        BY_PERIOD[702] = [1, 3, 4, 5, 6]
        try:
            ((_, txns),) = await get_unreconciled_transactions(client, JUL, SEP_END)
            assert len({t.id for t in txns}) == len(txns)
        finally:
            BY_PERIOD[702] = [3, 4, 5, 6]

    async def test_no_periods_is_empty_not_an_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/ledger/account":
                return httpx.Response(200, json={"values": [ACCOUNT], "fullResultSize": 1})
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        c = TripletexClient(TripletexConfig(base_url=BASE_URL))
        c._session = ApiSession(session_token="tok", company_id=0)
        c._http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BASE_URL
        )

        assert await get_unreconciled_transactions(c, JUL, SEP_END) == []
