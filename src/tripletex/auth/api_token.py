"""Official Tripletex API authentication via consumer/employee tokens."""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from tripletex.session import ApiSession


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
        expiration_date = date.today() + timedelta(days=1)

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
