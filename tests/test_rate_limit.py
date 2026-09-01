"""Rate limiter tests. Time is injected, so nothing here actually sleeps.

Ported from the ops-monitor project along with the limiter itself, using
httpx.MockTransport rather than pytest-httpx to avoid a new dev dependency.
"""

from __future__ import annotations

import httpx
import pytest

from tripletex.rate_limit import (
    FALLBACK_COOLDOWN,
    RateLimiter,
    send,
)

URL = "https://x.test/v2/thing"


class FakeClock:
    """A monotonic clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _limiter(clock: FakeClock, **kw) -> RateLimiter:
    return RateLimiter(clock=clock, sleep=clock.sleep, **kw)


def _client(*responses: httpx.Response, base_url: str = "") -> httpx.AsyncClient:
    """Client that returns the given responses in order, repeating the last."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=base_url
    )


def _no_jitter(delay: float) -> float:
    return delay


class TestPacing:
    async def test_first_request_does_not_wait(self, clock: FakeClock) -> None:
        await _limiter(clock).acquire()
        assert clock.slept == []

    async def test_successive_requests_are_paced(self, clock: FakeClock) -> None:
        limiter = _limiter(clock, min_interval=0.1)
        await limiter.acquire()
        await limiter.acquire()
        # Second call waits out the interval since no time passed on its own.
        assert clock.slept == [pytest.approx(0.1)]

    async def test_above_the_floor_does_not_stall(self, clock: FakeClock) -> None:
        limiter = _limiter(clock, floor=20)
        limiter.observe({"x-rate-limit-remaining": "99", "x-rate-limit-reset": "9"})
        await limiter.acquire()
        assert clock.slept == []


class TestHeaders:
    async def test_headers_are_recorded(self, clock: FakeClock) -> None:
        limiter = _limiter(clock)
        limiter.observe(
            {
                "x-rate-limit-limit": "100",
                "x-rate-limit-remaining": "60",
                "x-rate-limit-reset": "4",
            }
        )
        assert (limiter.limit, limiter.remaining) == (100, 60)

    async def test_unparseable_headers_keep_previous_state(
        self, clock: FakeClock
    ) -> None:
        limiter = _limiter(clock)
        limiter.observe({"x-rate-limit-remaining": "60"})
        limiter.observe({"x-rate-limit-remaining": "not-a-number"})
        assert limiter.remaining == 60


class TestFloor:
    async def test_waits_out_the_window_at_the_floor(self, clock: FakeClock) -> None:
        limiter = _limiter(clock, floor=20)
        limiter.observe({"x-rate-limit-remaining": "20", "x-rate-limit-reset": "7"})
        await limiter.acquire()
        # Reserves the last 20 requests for interactive users rather than
        # spending them.
        assert clock.slept == [pytest.approx(7.0)]

    async def test_floor_without_a_reset_header_uses_the_fallback(
        self, clock: FakeClock
    ) -> None:
        limiter = _limiter(clock, floor=20)
        limiter.observe({"x-rate-limit-remaining": "5"})
        await limiter.acquire()
        assert clock.slept == [pytest.approx(FALLBACK_COOLDOWN)]

    async def test_the_fallback_cooldown_is_paid_once_not_per_request(
        self, clock: FakeClock
    ) -> None:
        """Regression: `remaining` stays below the floor until a response
        updates it, so a fallback that records no deadline re-triggers the full
        pause on every acquire — 4 requests cost 40s instead of 10."""
        limiter = _limiter(clock, floor=20, min_interval=0.1)
        limiter.observe({"x-rate-limit-remaining": "5"})  # no reset header

        for _ in range(4):
            await limiter.acquire()

        assert sum(clock.slept) == pytest.approx(FALLBACK_COOLDOWN + 0.3)

    async def test_observed_reset_is_also_paid_once(self, clock: FakeClock) -> None:
        limiter = _limiter(clock, floor=20, min_interval=0.1)
        limiter.observe({"x-rate-limit-remaining": "5", "x-rate-limit-reset": "7"})

        for _ in range(4):
            await limiter.acquire()

        assert sum(clock.slept) == pytest.approx(7.0 + 0.3)


class TestSend:
    async def test_success_returns_the_response(self, clock: FakeClock) -> None:
        limiter = _limiter(clock)
        response = httpx.Response(
            200,
            json={"values": []},
            headers={"x-rate-limit-remaining": "98", "x-rate-limit-reset": "9"},
        )
        async with _client(response) as client:
            result = await send(client, limiter, "GET", URL, jitter=_no_jitter)

        assert result.status_code == 200
        # State from the response is folded back into the limiter.
        assert limiter.remaining == 98

    async def test_429_is_retried_honouring_retry_after(
        self, clock: FakeClock
    ) -> None:
        limiter = _limiter(clock)
        async with _client(
            httpx.Response(429, headers={"retry-after": "3"}),
            httpx.Response(200, json={"ok": True}),
        ) as client:
            result = await send(client, limiter, "GET", URL, jitter=_no_jitter)

        assert result.status_code == 200
        assert 3.0 in clock.slept

    async def test_429_without_retry_after_backs_off_exponentially(
        self, clock: FakeClock
    ) -> None:
        limiter = _limiter(clock)
        async with _client(
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, json={"ok": True}),
        ) as client:
            await send(client, limiter, "GET", URL, jitter=_no_jitter)

        assert [s for s in clock.slept if s in (1.0, 2.0)] == [1.0, 2.0]

    async def test_a_post_is_never_replayed_after_an_ambiguous_failure(
        self, clock: FakeClock
    ) -> None:
        """The client POSTs to /v2/invoice, /v2/order and /v2/customer. A 503
        may mean the document was created and the response died on the way
        back, and Tripletex has no idempotency key, so a replay duplicates it
        for real."""
        limiter = _limiter(clock)
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await send(
                client, limiter, "POST", URL, max_attempts=3, jitter=_no_jitter
            )

        assert result.status_code == 503
        assert attempts == 1
        assert clock.slept == []

    async def test_a_post_is_retried_on_429(self, clock: FakeClock) -> None:
        """Being throttled is a refusal, not an ambiguous outcome — the request
        was never processed, so replaying it cannot duplicate anything."""
        limiter = _limiter(clock)
        async with _client(
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json={"id": 1}),
        ) as client:
            result = await send(
                client, limiter, "POST", URL, jitter=_no_jitter
            )

        assert result.status_code == 200
        assert 2.0 in clock.slept

    @pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE"])
    async def test_idempotent_methods_are_retried_on_503(
        self, clock: FakeClock, method: str
    ) -> None:
        limiter = _limiter(clock)
        async with _client(
            httpx.Response(503), httpx.Response(200)
        ) as client:
            result = await send(client, limiter, method, URL, jitter=_no_jitter)

        assert result.status_code == 200

    async def test_a_bare_500_is_not_retried(self, clock: FakeClock) -> None:
        """Usually deterministic — the server choked on this exact request, so
        three attempts just make the same failure slower."""
        limiter = _limiter(clock)
        async with _client(httpx.Response(500)) as client:
            result = await send(client, limiter, "GET", URL, jitter=_no_jitter)

        assert result.status_code == 500
        assert clock.slept == []

    async def test_5xx_is_retried_then_returned(self, clock: FakeClock) -> None:
        """The caller raises, not send() — that is what keeps the 401 branch in
        _request() able to produce SessionExpired."""
        limiter = _limiter(clock)
        async with _client(httpx.Response(503)) as client:
            result = await send(
                client, limiter, "GET", URL, max_attempts=3, jitter=_no_jitter
            )

        assert result.status_code == 503
        assert len(clock.slept) == 2  # retried twice, then gave up

    async def test_4xx_is_not_retried(self, clock: FakeClock) -> None:
        # A 403 means "token auth cannot reach this", not "try again".
        limiter = _limiter(clock)
        async with _client(httpx.Response(403)) as client:
            result = await send(client, limiter, "GET", URL, jitter=_no_jitter)

        assert result.status_code == 403
        assert clock.slept == []

    async def test_max_attempts_below_one_is_rejected(self, clock: FakeClock) -> None:
        # Otherwise the loop never runs and there is no response to return.
        limiter = _limiter(clock)
        async with _client(httpx.Response(200)) as client:
            with pytest.raises(ValueError, match="at least 1"):
                await send(client, limiter, "GET", URL, max_attempts=0)

    async def test_jitter_spreads_the_backoff(self, clock: FakeClock) -> None:
        """A fan-out across companies must not retry in lockstep."""
        limiter = _limiter(clock)
        async with _client(
            httpx.Response(429), httpx.Response(200)
        ) as client:
            await send(
                client, limiter, "GET", URL, jitter=lambda d: d * 1.5
            )

        assert clock.slept == [pytest.approx(1.5)]


class TestClientIntegration:
    async def test_a_401_still_becomes_session_expired_through_the_limiter(
        self,
    ) -> None:
        """The limiter must not swallow the typed auth failure."""
        import httpx as _httpx

        from tripletex.client import TripletexClient
        from tripletex.config import TripletexConfig
        from tripletex.session import SessionExpired, WebSession

        cookies = _httpx.Cookies()
        cookies.set("CSRFTokenWriteOnly", "x", domain="tripletex.no", path="/")

        client = TripletexClient(TripletexConfig(base_url="https://tripletex.no"))
        client._session = WebSession(cookies=cookies, context_id="1")
        client._http = _client(
            httpx.Response(401), base_url="https://tripletex.no"
        )

        with pytest.raises(SessionExpired):
            await client.get_json("/v2/bank/payment")
