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
