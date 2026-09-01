"""Token-bucket pacing for Tripletex, plus retry on 429/5xx.

Originally written for the ops-monitor project and imported here so the CLI
and any service share one implementation. There is one limiter per client, so
every query on the same credential shares it — which is what stops an
`asyncio.gather` fan-out from stampeding.

Tripletex documents the quota as counted "on the API calls for an employee for
each API consumer" — so the budget belongs to the *(employee token, consumer
token)* pair. Clients sharing a consumer token but using different employee
tokens therefore hold independent budgets, which is what makes one limiter per
client the right granularity. Two clients built from the *same* credentials
should share one, hence the settable `limiter` property on the client.

Web sessions are not covered by that statement — they present cookies and a
context id, never the tokens — so whether they are metered at all is unmeasured.

Measured limits (`docs/adapter-notes.md`): ~100 requests per rolling ~10 s
window, roughly 10 req/s sustained. Every response carries the current state,
so the limiter steers by observation rather than a fixed budget:

    x-rate-limit-limit: 100
    x-rate-limit-remaining: 99..60
    x-rate-limit-reset: 9..4        (seconds until the window resets)

It deliberately stops short of exhausting the quota. Humans use Tripletex
interactively against the same token, and a monitor that consumes the last
request makes the accounting system feel broken.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Leave this much headroom for interactive use by humans.
DEFAULT_FLOOR = 20
# ~10 req/s sustained.
DEFAULT_MIN_INTERVAL = 0.1
DEFAULT_MAX_ATTEMPTS = 3
# Fallback pause when we must back off but the server gave us no reset hint.
FALLBACK_COOLDOWN = 10.0
# Backoff is spread by up to this fraction so that a fan-out across companies
# does not retry in lockstep.
DEFAULT_JITTER = 0.1

#: Methods safe to replay after an *ambiguous* failure — one where the server
#: may have committed the work and then failed while answering. POST is absent
#: deliberately: this client creates invoices, orders, products and customers,
#: and Tripletex offers no idempotency key, so a replayed POST can duplicate a
#: real document. Matches urllib3's `Retry.DEFAULT_ALLOWED_METHODS`.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE"})

#: Ambiguous failures worth retrying, for an idempotent method. A gateway or an
#: overloaded backend is transient. A bare 500 is not here: it usually means the
#: server choked on this specific request — a `fields` combination it dislikes,
#: say — so retrying repeats a deterministic failure and only adds latency.
AMBIGUOUS_STATUSES = frozenset({502, 503, 504})

#: Rate limiting is a *refusal*: the request was never processed, so replaying
#: it cannot duplicate anything. Safe on any method, POST included — which
#: matters, since being throttled is the condition this module exists for.
RATE_LIMIT_STATUS = 429

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
Jitter = Callable[[float], float]


def _default_jitter(delay: float) -> float:
    return delay * (1 + random.uniform(0, DEFAULT_JITTER))


class RateLimiter:
    """Paces requests against one Tripletex token.

    `clock` and `sleep` are injectable so tests can drive time directly
    instead of actually waiting.
    """

    def __init__(
        self,
        *,
        floor: int = DEFAULT_FLOOR,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self.floor = floor
        self.min_interval = min_interval
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self.sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._next_earliest = 0.0
        self.remaining: int | None = None
        self.limit: int | None = None
        # Absolute time (on `clock`) when the current window resets.
        self._resets_at: float | None = None

    async def acquire(self) -> None:
        """Block until it is polite to send the next request."""
        async with self._lock:
            wait = max(0.0, self._next_earliest - self._clock())

            # Below the floor, wait out the window rather than spend the
            # headroom reserved for interactive users.
            if self.remaining is not None and self.remaining <= self.floor:
                cooldown = self._cooldown()
                if cooldown > wait:
                    logger.info(
                        "rate limit floor reached (remaining=%s), pausing %.1fs",
                        self.remaining,
                        cooldown,
                    )
                    wait = cooldown

            if wait > 0:
                await self.sleep(wait)

            self._next_earliest = self._clock() + self.min_interval

    def _cooldown(self) -> float:
        """Seconds to wait out the current window.

        When the server gave no reset hint we invent one and record it, so the
        wait happens once. Leaving `_resets_at` unset would re-trigger the full
        fallback pause on every subsequent acquire — the floor is sticky until a
        response updates `remaining`, and without a recorded deadline nothing
        ever makes the branch stop firing.
        """
        if self._resets_at is None:
            self._resets_at = self._clock() + FALLBACK_COOLDOWN
        return max(0.0, self._resets_at - self._clock())

    def observe(self, headers: Mapping[str, str]) -> None:
        """Update state from a response's rate-limit headers."""
        self.limit = _int_header(headers, "x-rate-limit-limit", self.limit)
        self.remaining = _int_header(headers, "x-rate-limit-remaining", self.remaining)
        reset = _int_header(headers, "x-rate-limit-reset", None)
        if reset is not None:
            self._resets_at = self._clock() + reset


def _int_header(
    headers: Mapping[str, str], name: str, default: int | None
) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("unparseable %s header: %r", name, raw)
        return default


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying.

    Whether Tripletex sends `Retry-After` on a 429 is untested against
    production (`docs/adapter-notes.md`), so honour it when present and fall
    back to exponential backoff when it is not.
    """
    raw = response.headers.get("retry-after")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            # Retry-After may also be an HTTP-date, which we do not parse.
            logger.warning("unparseable retry-after header: %r", raw)
    return float(2**attempt)


def should_retry(method: str, status_code: int) -> bool:
    """Whether a failed attempt may be replayed.

    Turns on the difference between "refused" and "ambiguous": a 429 means the
    request was never processed, while a 502/503/504 means it might have been.
    Only the first is safe for a method that changes state.
    """
    if status_code == RATE_LIMIT_STATUS:
        return True
    if status_code not in AMBIGUOUS_STATUSES:
        return False
    return method.upper() in IDEMPOTENT_METHODS


async def send(
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    method: str,
    url: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    jitter: Jitter | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send one request, paced and retried, and return the final response.

    Retries up to `max_attempts`, but only what `should_retry` allows: a 429 on
    any method, and 502/503/504 on idempotent methods alone. A POST is never
    replayed after an ambiguous failure — it may have created a document.

    **Does not raise for status** — the caller decides what a status means,
    which is what lets `_request()` turn a 401 on a web session into
    `SessionExpired` rather than a bare `HTTPStatusError`. Connection errors and
    timeouts are not retried; they propagate.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
    spread = jitter or _default_jitter

    response: httpx.Response | None = None
    for attempt in range(max_attempts):
        await limiter.acquire()
        response = await client.request(method, url, **kwargs)
        limiter.observe(response.headers)

        if not should_retry(method, response.status_code):
            return response

        if attempt < max_attempts - 1:
            delay = spread(_retry_after(response, attempt))
            logger.warning(
                "%s %s -> %s, retrying in %.1fs (attempt %d/%d)",
                method,
                url,
                response.status_code,
                delay,
                attempt + 1,
                max_attempts,
            )
            await limiter.sleep(delay)

    # Every attempt failed; hand back the last response so the caller can
    # raise with its own context.
    assert response is not None  # guaranteed by the max_attempts >= 1 check
    return response
