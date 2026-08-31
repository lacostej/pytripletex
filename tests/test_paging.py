"""Tests for the list-endpoint pagination helper.

The fake client reproduces Tripletex's real envelope: `fullResultSize` is
`min(total, count) + 1` on most endpoints — a has-more flag, not a total — so a
pager that trusts it stops early. These tests pin the short-page rule instead.
"""

from tripletex.endpoints._paging import paginate


class FakeClient:
    """Serves `total` rows, capping fullResultSize the way Tripletex does."""

    def __init__(self, total: int, server_max_page: int | None = None):
        self.total = total
        self.server_max_page = server_max_page
        self.calls: list[tuple[int, int]] = []

    def _page(self, params):
        offset, count = int(params["from"]), int(params["count"])
        if self.server_max_page is not None:
            count = min(count, self.server_max_page)
        self.calls.append((offset, count))
        values = [{"id": i} for i in range(offset, min(offset + count, self.total))]
        return {"values": values, "fullResultSize": min(self.total, count) + 1}

    async def get_json(self, path, params=None):
        return self._page(params)

    async def post_json(self, path, params=None, json_body=None):
        self.body = json_body
        return self._page(params)


async def test_single_page_when_everything_fits():
    client = FakeClient(total=65)
    rows = await paginate(client, "/v2/employee", page_size=1000)
    assert len(rows) == 65
    assert client.calls == [(0, 1000)]


async def test_pages_past_a_lying_full_result_size():
    # 2453 rows in pages of 1000: a pager trusting fullResultSize would stop at 1000.
    client = FakeClient(total=2453)
    rows = await paginate(client, "/v2/ledger/voucher", page_size=1000)
    assert len(rows) == 2453
    assert [r["id"] for r in rows] == list(range(2453))
    assert client.calls == [(0, 1000), (1000, 1000), (2000, 1000)]


async def test_exact_multiple_needs_a_final_short_page():
    client = FakeClient(total=100)
    rows = await paginate(client, "/v2/customer", page_size=50)
    assert len(rows) == 100
    # Third request confirms the end; without it we could not tell 100 from 150.
    assert client.calls == [(0, 50), (50, 50), (100, 50)]


async def test_limit_stops_early_and_does_not_overfetch():
    client = FakeClient(total=2453)
    rows = await paginate(client, "/v2/ledger/voucher", page_size=1000, limit=1500)
    assert len(rows) == 1500
    assert client.calls == [(0, 1000), (1000, 500)]


async def test_empty_result_makes_one_call():
    client = FakeClient(total=0)
    rows = await paginate(client, "/v2/invoice", page_size=1000)
    assert rows == []
    assert client.calls == [(0, 1000)]


async def test_server_capped_page_size_still_terminates():
    # The inbox endpoint serves at most 50 rows however many you ask for.
    client = FakeClient(total=120, server_max_page=50)
    rows = await paginate(client, "/v2/voucherInbox/inboxFiltered", page_size=50)
    assert len(rows) == 120


class IgnoresPagingClient:
    """Serves the whole set every request, like /v2/ledger/voucher/>nonPosted.

    Ignores `from` and `count`, and reports fullResultSize=0.
    """

    def __init__(self, total: int):
        self.total = total
        self.calls = 0

    async def get_json(self, path, params=None):
        self.calls += 1
        return {"values": [{"id": i} for i in range(self.total)], "fullResultSize": 0}


async def test_endpoint_ignoring_count_returns_everything_once():
    client = IgnoresPagingClient(total=24)
    rows = await paginate(client, "/v2/ledger/voucher/>nonPosted", page_size=1000)
    assert len(rows) == 24
    assert client.calls == 1


async def test_endpoint_ignoring_paging_does_not_loop_or_duplicate():
    # The dangerous case: the full set is larger than one page, so a naive pager
    # would advance `from`, get the same rows back, and never terminate.
    client = IgnoresPagingClient(total=2500)
    rows = await paginate(client, "/v2/ledger/voucher/>nonPosted", page_size=1000)
    assert len(rows) == 2500
    assert [r["id"] for r in rows] == list(range(2500))
    assert client.calls == 1


async def test_identical_page_twice_stops_without_duplicating():
    # Exactly page-size rows: the long-page rule cannot fire, so the repeated-page
    # check is what stops it.
    client = IgnoresPagingClient(total=1000)
    rows = await paginate(client, "/v2/ledger/voucher/>nonPosted", page_size=1000)
    assert len(rows) == 1000
    assert client.calls == 2


async def test_limit_is_honoured_even_when_the_endpoint_overserves():
    client = IgnoresPagingClient(total=2500)
    rows = await paginate(client, "/v2/ledger/voucher/>nonPosted", page_size=1000, limit=10)
    assert len(rows) == 10


async def test_extra_params_are_preserved_and_post_body_passed_through():
    client = FakeClient(total=3)
    rows = await paginate(
        client,
        "/v2/salary/employee/overview/details",
        params={"fields": "id,displayName"},
        json_body={"query": ""},
        page_size=1000,
    )
    assert len(rows) == 3
    assert client.body == {"query": ""}


class RecordingClient:
    """Captures the request the non-posted queue endpoint receives."""

    def __init__(self, values):
        self.values = values
        self.path = None
        self.params = None

    async def get_json(self, path, params=None):
        self.path, self.params = path, params
        return {"values": self.values, "fullResultSize": 0}


async def test_non_posted_queue_sends_the_filters_the_endpoint_expects():
    from tripletex.endpoints.vouchers import list_non_posted_vouchers

    client = RecordingClient([
        {"id": 1, "number": 0, "tempNumber": 17471, "date": "2026-08-27",
         "description": "Lightspeed", "attachment": None},
    ])
    vouchers = await list_non_posted_vouchers(
        client, changed_since="2026-08-25T00:00:00Z"
    )

    assert client.path == "/v2/ledger/voucher/>nonPosted"
    # includeNonApproved is mandatory; a bare date in changedSince gets a 422, so
    # the value is passed through untouched rather than reformatted.
    assert client.params["includeNonApproved"] == "true"
    assert client.params["changedSince"] == "2026-08-25T00:00:00Z"
    # Unposted vouchers carry number 0 — the temp number is the useful identity.
    assert vouchers[0].number == 0
    assert vouchers[0].temp_number == 17471
    assert vouchers[0].display_number == "T17471"


async def test_reception_listing_hits_the_documented_endpoint():
    from tripletex.endpoints.vouchers import list_reception_vouchers

    client = RecordingClient([
        {"id": 673865149, "number": 0, "tempNumber": 17483, "date": "2026-08-30",
         "description": "Faktura nummer 24007 fra Villa Import AS",
         "attachment": {"id": 1158950306, "fileName": "invoice-24007.pdf"}},
    ])
    vouchers = await list_reception_vouchers(client, search_text="Villa")

    assert client.path == "/v2/ledger/voucher/>voucherReception"
    assert client.params["searchText"] == "Villa"
    # Reception rows are unposted, so the temp number is the identity, and the
    # attachment is what distinguishes them from the >nonPosted queue.
    assert vouchers[0].display_number == "T17483"
    assert vouchers[0].document_ids == [1158950306]
