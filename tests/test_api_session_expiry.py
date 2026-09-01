"""Session expiry is stamped in Norwegian time, not the host's.

Tripletex reads `expirationDate` in Europe/Oslo and treats it as the moment the
session stops working. Stamping `today + 1` from a UTC host issued a session
that was already dead for the last two hours of every UTC day: the create call
succeeded, every request with the session answered 401, and it repaired itself
at midnight.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from tripletex.auth.api_token import (
    EXPIRATION_DAYS,
    create_api_session,
    default_expiration,
)

OSLO = ZoneInfo("Europe/Oslo")
BASE_URL = "https://tripletex.no"


class TestDefaultExpiration:
    def test_counts_from_the_given_day(self):
        assert default_expiration(date(2026, 9, 1)) == date(2026, 9, 3)

    def test_the_reported_outage_window(self):
        """2026-09-01 22:39 UTC: the host still reads 09-01 while Oslo has
        already rolled over to 09-02. The old code stamped 09-02 — a day that
        had already begun in Oslo — and every request answered 401."""
        moment = datetime(2026, 9, 1, 22, 39, tzinfo=timezone.utc)
        assert moment.astimezone(OSLO).date() == date(2026, 9, 2)  # Oslo is ahead

        stamped = default_expiration(moment.date())

        assert stamped == date(2026, 9, 3)
        assert stamped > moment.astimezone(OSLO).date()

    def test_defaults_to_the_utc_date_not_the_hosts(self):
        # Whatever TZ the machine is set to, the answer is the same.
        assert default_expiration() == (
            datetime.now(timezone.utc).date() + timedelta(days=EXPIRATION_DAYS)
        )

    @pytest.mark.parametrize("hour", range(24))
    @pytest.mark.parametrize(
        "day",
        [
            date(2026, 1, 15),  # CET, UTC+1
            date(2026, 3, 29),  # spring forward
            date(2026, 7, 1),  # CEST, UTC+2
            date(2026, 10, 25),  # fall back
            date(2026, 12, 31),  # year boundary
        ],
    )
    def test_always_outlives_the_current_oslo_day(self, day: date, hour: int):
        """The invariant that matters: whatever the hour and whichever side of a
        DST change, the stamped date is strictly after the Oslo date at the
        moment of creation — so the session is alive when it is handed over."""
        moment = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)

        stamped = default_expiration(moment.date())

        assert stamped > moment.astimezone(OSLO).date()

    @pytest.mark.parametrize("hour", range(24))
    def test_the_old_one_day_rule_would_have_failed(self, hour: int):
        """Proves the test above has teeth: the previous rule breaks in exactly
        the last two hours of the UTC day, and only there."""
        moment = datetime(2026, 9, 1, hour, tzinfo=timezone.utc)
        old_rule = moment.date() + timedelta(days=1)

        alive = old_rule > moment.astimezone(OSLO).date()

        assert alive is (hour < 22)

    def test_shortest_session_is_still_most_of_a_day(self):
        """Worst case: created a minute before the UTC day ends, when Oslo has
        already rolled over."""
        moment = datetime(2026, 9, 1, 23, 59, tzinfo=timezone.utc)
        expires_at = datetime.combine(
            default_expiration(moment.date()), datetime.min.time(), tzinfo=OSLO
        )

        assert expires_at - moment > timedelta(hours=22)


class TestCreateApiSession:
    @respx.mock
    async def test_stamps_the_safe_expiry_by_default(self):
        route = respx.put(f"{BASE_URL}/v2/token/session/:create").mock(
            return_value=httpx.Response(200, json={"value": {"token": "sess-tok"}})
        )

        session = await create_api_session(
            BASE_URL, consumer_token="c", employee_token="e"
        )

        assert session.session_token == "sess-tok"
        sent = route.calls.last.request.url.params["expirationDate"]
        assert sent == default_expiration().isoformat()

    @respx.mock
    async def test_an_explicit_expiry_is_still_honoured(self):
        route = respx.put(f"{BASE_URL}/v2/token/session/:create").mock(
            return_value=httpx.Response(200, json={"value": {"token": "t"}})
        )

        await create_api_session(
            BASE_URL,
            consumer_token="c",
            employee_token="e",
            expiration_date=date(2026, 12, 24),
        )

        assert route.calls.last.request.url.params["expirationDate"] == "2026-12-24"
