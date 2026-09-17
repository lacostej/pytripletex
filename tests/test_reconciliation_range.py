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
from tripletex.endpoints.reconciliation import (
    get_unreconciled_transactions,
    periods_covering,
)
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


def _filter_periods(params) -> list[dict]:
    """The endpoint's own filter semantics, so the mock cannot be kinder than
    the API. Every bound is half-open: `*From` includes, `*To` excludes."""
    rows = PERIODS
    if "startFrom" in params:
        rows = [p for p in rows if p["start"] >= params["startFrom"]]
    if "startTo" in params:
        rows = [p for p in rows if p["start"] < params["startTo"]]
    if "endFrom" in params:
        rows = [p for p in rows if p["end"] >= params["endFrom"]]
    if "endTo" in params:
        rows = [p for p in rows if p["end"] < params["endTo"]]
    return rows


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
            # Filters on the period's *start* date, as the real endpoint does.
            # A mock that ignores the parameters cannot show the bug the
            # parameters cause.
            rows = _filter_periods(request.url.params)
            return httpx.Response(200, json={"values": rows, "fullResultSize": len(rows)})
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

    async def test_a_mid_month_range_still_sweeps_its_first_period(self):
        """`collect()` computes `today - 31*months`, which lands mid-month. With
        a start-date filter the oldest period silently drops out — so the sweep
        must ask for periods that *overlap*, not periods that start inside."""
        ((_, txns),) = await get_unreconciled_transactions(
            _client(), datetime.date(2026, 7, 15), SEP_END
        )

        assert 1 in {t.id for t in txns}, "July's transactions must survive"
        assert len(txns) == 12

    async def test_a_range_inside_a_single_period_is_not_empty(self):
        """A week in July matches no period *start*, so the old behaviour
        returned nothing — which a monitor reads as 'nothing outstanding'."""
        results = await get_unreconciled_transactions(
            _client(), datetime.date(2026, 7, 15), datetime.date(2026, 7, 20)
        )

        assert results, "a week inside July must still find July"

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


class TestPeriodsCovering:
    """`get_periods` filters on the period's *start* date, which is rarely what
    a caller means and fails silently two ways."""

    def _periods_client(self, seen: list | None = None):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/ledger/accountingPeriod":
                if seen is not None:
                    seen.append(dict(request.url.params))
                rows = _filter_periods(request.url.params)
                return httpx.Response(200, json={"values": rows, "fullResultSize": len(rows)})
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        c = TripletexClient(TripletexConfig(base_url=BASE_URL))
        c._session = ApiSession(session_token="tok", company_id=0)
        c._http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BASE_URL
        )
        return c

    async def test_a_mid_month_start_keeps_its_own_period(self):
        """A range computed as `today - N days` lands mid-month. Filtering on
        the start date drops the period that range begins inside."""
        got = await periods_covering(
            self._periods_client(), datetime.date(2026, 7, 15), datetime.date(2026, 9, 30)
        )

        assert [p.id for p in got] == [701, 702, 703]

    async def test_a_range_inside_one_period_finds_that_period(self):
        """The dangerous case: no period *starts* in this week, so the filtered
        query returns nothing, and nothing reads as "nothing outstanding"."""
        got = await periods_covering(
            self._periods_client(), datetime.date(2026, 7, 15), datetime.date(2026, 7, 20)
        )

        assert [p.id for p in got] == [701]

    async def test_overlap_is_asked_of_the_server_not_filtered_locally(self):
        """`endFrom`/`startTo` expresses overlap exactly, so this is one query
        with no local filtering and no guess at how long a period can be."""
        seen: list[dict] = []

        await periods_covering(
            self._periods_client(seen), datetime.date(2026, 7, 15), datetime.date(2026, 7, 20)
        )

        assert len(seen) == 1
        assert seen[0]["endFrom"] == "2026-07-16", "period must end after the range starts"
        assert seen[0]["startTo"] == "2026-07-21", "'to' excludes, so +1 day"
        assert "startFrom" not in seen[0], "filtering on start is the bug"

    async def test_periods_after_the_range_are_excluded(self):
        got = await periods_covering(
            self._periods_client(), datetime.date(2026, 7, 1), datetime.date(2026, 7, 31)
        )

        assert [p.id for p in got] == [701]

    async def test_the_end_date_is_treated_as_exclusive(self):
        """A period reads 2026-07-01..2026-08-01. A range starting exactly on
        2026-08-01 is in August, not July."""
        got = await periods_covering(
            self._periods_client(), datetime.date(2026, 8, 1), datetime.date(2026, 8, 15)
        )

        assert [p.id for p in got] == [702]
