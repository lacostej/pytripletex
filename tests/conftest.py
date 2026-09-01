"""Shared test fixtures."""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _unpaced_client(monkeypatch):
    """Make clients built inside tests wait for nothing.

    Retry, floor and header handling are all still real — only the waiting is
    removed. Without this a 0.1s gap per request plus a 1s+2s retry backoff
    turns a 0.2s suite into a 3s one and buys no coverage. Tests that assert on
    timing construct their own limiter with an injected clock, so they are
    unaffected by this.
    """
    from tripletex import rate_limit

    async def _no_wait(seconds: float) -> None:
        return None

    class _Unpaced(rate_limit.RateLimiter):
        def __init__(self, **kwargs):
            kwargs.setdefault("min_interval", 0.0)
            kwargs.setdefault("sleep", _no_wait)
            super().__init__(**kwargs)

    monkeypatch.setattr("tripletex.client.RateLimiter", _Unpaced)


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()
