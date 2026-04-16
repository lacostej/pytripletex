"""Company listing endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tripletex.models import Company

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def list_companies(client: TripletexClient) -> list[Company]:
    """List all companies accessible to the current session.

    Calls GET /v2/internal/company-chooser/companies. The response does
    not include organizationNumber — use ``get_company()`` to enrich.
    """
    data = await client.get_json("/v2/internal/company-chooser/companies")
    raw_companies = data.get("value", data.get("values", []))
    return [Company.model_validate(c) for c in raw_companies]


async def get_company(client: TripletexClient, company_id: int) -> Company:
    """Fetch full details for a single company (including organizationNumber).

    The GET /v2/company/{id} endpoint only works when the session context
    matches the requested company, so this temporarily switches context.
    """
    from tripletex.session import WebSession

    session = client.session
    if isinstance(session, WebSession):
        original_context = session.context_id
        session.context_id = str(company_id)
        try:
            data = await client.get_json(f"/v2/company/{company_id}")
        finally:
            session.context_id = original_context
    else:
        data = await client.get_json(f"/v2/company/{company_id}")
    return Company.model_validate(data["value"])


async def find_company_by_organization_number(
    client: TripletexClient, organization_number: str
) -> Company | None:
    """Find a company in the chooser list matching the given org number.

    Fetches full details for each accessible company until a match is
    found. Returns None if none match.
    """
    companies = await list_companies(client)
    for c in companies:
        full = await get_company(client, c.id)
        if full.organization_number == organization_number:
            return full
    return None
