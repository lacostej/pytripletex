"""Official Tripletex API authentication via consumer/employee tokens."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx

from tripletex.session import ApiSession

#: How long a newly created session lasts, in days.
#:
#: Two, not one, and the reason is an outage rather than caution. Tripletex
#: reads `expirationDate` in Norwegian time and treats it as the instant the
#: session **stops** working, not the last day it works — a session stamped
#: 2026-09-02 is already dead at 00:00 Oslo on 2026-09-02.
#:
#: Oslo runs UTC+1 or UTC+2, so its date is either the current UTC date or the
#: next one, never further. Adding two days therefore always lands at least one
#: full Oslo day ahead, whatever the hour. Adding one does not: for the last two
#: hours of the UTC day, Oslo has already rolled over, so `today + 1` names a day
#: that has *already begun* there. The session is issued dead, every request with
#: it answers 401, and the handshake that created it still reports success — so
#: it presents as a revoked credential and repairs itself at midnight.
EXPIRATION_DAYS = 2


def default_expiration(today: date | None = None) -> date:
    """The expiry to stamp on a session created now.

    Counts from the UTC date rather than the host's, so the answer does not
    depend on how the machine happens to be configured. The shortest session
    this can produce is about 22 hours, which is ample: sessions are created per
    process and used for seconds.

    Converting to `Europe/Oslo` and adding a single day would be exact to the
    day, but leaves a session created at 23:59 Oslo expiring a minute later — a
    narrower version of the same bug — and needs a tz database that slim
    containers do not always carry.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    return today + timedelta(days=EXPIRATION_DAYS)


async def create_api_session(
    base_url: str,
    consumer_token: str,
    employee_token: str,
    expiration_date: date | None = None,
    company_id: int = 0,
) -> ApiSession:
    """Create an API session token.

    PUT /v2/token/session/:create?consumerToken=X&employeeToken=Y&expirationDate=Z
    """
    if expiration_date is None:
        expiration_date = default_expiration()

    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.put(
            f"{base_url}/v2/token/session/:create",
            params={
                "consumerToken": consumer_token,
                "employeeToken": employee_token,
                "expirationDate": expiration_date.isoformat(),
            },
        )
        response.raise_for_status()
        data = response.json()

    token = data["value"]["token"]
    return ApiSession(session_token=token, company_id=company_id)
