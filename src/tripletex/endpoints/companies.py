"""Company listing endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tripletex.models import Company

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def list_companies(client: TripletexClient) -> list[Company]:
    """List all companies accessible to the current session.

    Calls GET /v2/internal/company-chooser/companies
    """
    data = await client.get_json("/v2/internal/company-chooser/companies")
    raw_companies = data.get("value", data.get("values", []))
    return [Company.model_validate(c) for c in raw_companies]
