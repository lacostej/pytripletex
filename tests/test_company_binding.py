"""Tests for per-company clients.

Company switching used to mutate `session.context_id` and restore it on exit,
which made it unusable concurrently and quietly reverted any client held past
the switch. These cover the properties that replaced it.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.models import Company
from tripletex.session import ApiSession, WebSession, WebSessionRequired

BASE_URL = "https://tripletex.no"
HANDEL = Company(id=32611682, displayName="Bonita Handel AS")
SERVICES = Company(id=56801690, displayName="Bonita Services AS")


def _client(**kwargs) -> TripletexClient:
    cookies = httpx.Cookies()
    cookies.set("JSESSIONID", "sess", domain="tripletex.no", path="/")
    cookies.set("CSRFTokenWriteOnly", "csrf", domain="tripletex.no", path="/")
    client = TripletexClient(TripletexConfig(base_url=BASE_URL), **kwargs)
    client._session = WebSession(cookies=cookies, context_id="1")
    return client


def _recording_transport(seen: list[str]) -> httpx.MockTransport:
    """Records the context id each request was sent with."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-tlx-context-id"])
        return httpx.Response(200, json={"values": []})

    return httpx.MockTransport(handler)


class TestBinding:
    def test_sibling_carries_the_new_company(self):
        client = _client()
        sibling = client.for_company(HANDEL)

        assert sibling.session.context_id == str(HANDEL.id)

    def test_the_original_is_untouched(self):
        """The whole point: no mutation, so nothing to restore."""
        client = _client()
        client.for_company(HANDEL)

        assert client.session.context_id == "1"

    def test_accepts_a_bare_id(self):
        # get_company() has an id, not a Company.
        assert _client().for_company(56801690).session.context_id == "56801690"

    def test_api_sessions_are_refused(self):
        client = TripletexClient(TripletexConfig())
        client._session = ApiSession(session_token="tok")

        with pytest.raises(WebSessionRequired):
            client.for_company(HANDEL)


class TestSharing:
    def test_login_is_shared(self):
        """Same cookie jar — a sibling must not need its own login."""
        client = _client()
        sibling = client.for_company(HANDEL)

        assert sibling.session.cookies is client.session.cookies

    def test_session_age_is_preserved(self):
        client = _client()
        assert client.for_company(HANDEL).session.created_at == (
            client.session.created_at
        )

    def test_connection_pool_is_shared(self):
        client = _client()
        assert client.for_company(HANDEL).http is client.http

    def test_rate_limiter_is_shared(self):
        """The quota belongs to the (employee, consumer) pair, not the company,
        so siblings must not each pace themselves independently."""
        client = _client()
        first = client.for_company(HANDEL)
        second = client.for_company(SERVICES)

        assert first.limiter is client.limiter
        assert second.limiter is client.limiter

    async def test_closing_a_sibling_does_not_break_the_original(self):
        client = _client()
        client._http = httpx.AsyncClient(base_url=BASE_URL)
        sibling = client.for_company(HANDEL)

        await sibling.close()

        assert not client.http.is_closed


class TestConcurrency:
    async def test_two_companies_can_run_at_once(self):
        """Under the old mutate-and-restore this returned the wrong company's
        data: the second switch overwrote the first before either finished."""
        seen: list[str] = []
        client = _client()
        client._http = httpx.AsyncClient(
            transport=_recording_transport(seen), base_url=BASE_URL
        )

        handel = client.for_company(HANDEL)
        services = client.for_company(SERVICES)
        await asyncio.gather(
            handel.get_json("/v2/ledger/voucher"),
            services.get_json("/v2/ledger/voucher"),
        )

        assert sorted(seen) == sorted([str(HANDEL.id), str(SERVICES.id)])

    async def test_a_collected_client_stays_bound(self):
        """iter_companies() yields pairs a caller may stash. Under the old
        version every stashed client reverted to the original company."""
        seen: list[str] = []
        client = _client()
        client._http = httpx.AsyncClient(
            transport=_recording_transport(seen), base_url=BASE_URL
        )

        siblings = [client.for_company(c) for c in (HANDEL, SERVICES)]
        # Used well after the loop that produced them.
        for sibling in siblings:
            await sibling.get_json("/v2/ledger/voucher")

        assert seen == [str(HANDEL.id), str(SERVICES.id)]


class TestCompanyContext:
    async def test_yields_a_bound_sibling_not_self(self):
        client = _client()

        async with client.company_context(HANDEL) as scoped:
            assert scoped is not client
            assert scoped.session.context_id == str(HANDEL.id)

    async def test_the_original_is_unchanged_afterwards(self):
        client = _client()

        async with client.company_context(HANDEL):
            pass

        assert client.session.context_id == "1"


class TestCliCompanyFlag:
    """`--company` is the site that would fail silently under a copy-based
    switch: the wrapper used to discard the yielded client and return the
    original, relying on the mutation having landed on it."""

    def _wrapper(self, monkeypatch, client, company_name="services"):
        from tripletex.cli import main as cli_main

        monkeypatch.setattr(cli_main, "_make_client", lambda ctx: client)

        async def fake_list_companies(self):
            return [HANDEL, SERVICES]

        monkeypatch.setattr(
            TripletexClient, "list_companies", fake_list_companies
        )

        ctx = type("Ctx", (), {})()
        ctx.obj = {"company_name": company_name, "config": client.config}
        return cli_main._CompanyClientWrapper(ctx)

    async def test_returns_a_client_bound_to_the_named_company(
        self, monkeypatch
    ):
        client = _client()

        async def already_authenticated():
            return client

        monkeypatch.setattr(client, "__aenter__", already_authenticated)
        wrapper = self._wrapper(monkeypatch, client)

        bound = await wrapper.__aenter__()

        assert bound.session.context_id == str(SERVICES.id)

    async def test_an_unknown_company_still_errors(self, monkeypatch):
        import click

        client = _client()

        async def already_authenticated():
            return client

        monkeypatch.setattr(client, "__aenter__", already_authenticated)
        wrapper = self._wrapper(monkeypatch, client, company_name="nope ltd")

        with pytest.raises(click.ClickException, match="not found"):
            await wrapper.__aenter__()
