"""Dashboard compliance reminders — statutory deadlines, web session only.

These are the filing and payment obligations Tripletex surfaces on its
dashboard: Skattemelding, A-melding, payment of arbeidsgiveravgift. They are
unrelated to `endpoints.invoices.list_reminders`, which chases customers for
money; the shared word "reminder" is Tripletex's, not ours.

**There is no API-token path to any of this**, measured 2026-09-03:

- `/v2/tripletexDashboard/newReminder` — 403 with a token, 200 with a session
- `/v2/globalReminder/{id}`, `/v2/companyReminder/{id}` — 403 with a token
- the list forms of both — 404, they do not exist
- none of the four paths appear in the official specification's 490 paths

So this joins payments, the voucher inbox and the internal salary endpoints on
the list of things a scheduler cannot reach without a human-established session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tripletex.models import DashboardReminder
from tripletex.session import require_web_session

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def list_dashboard_reminders(
    client: TripletexClient,
    urgent_only: bool = False,
) -> list[DashboardReminder]:
    """Statutory deadlines and how close they are.

    GET /v2/tripletexDashboard/newReminder. Note the response is keyed `value`
    rather than the usual `values`, and takes no paging parameters — it returns
    the company's current obligations and nothing else.

    `urgent_only` keeps the rows Tripletex itself colours red or yellow, which
    is the distinction a dashboard wants to act on.
    """
    require_web_session(client.session, "Dashboard reminders")

    data = await client.get_json("/v2/tripletexDashboard/newReminder")

    reminders: list[DashboardReminder] = []
    for row in data.get("value") or []:
        overall = row.get("globalReminder") or {}
        company = row.get("companyReminder") or {}
        reminders.append(
            DashboardReminder.model_validate(
                {
                    **overall,
                    # A missing companyReminder means the company has not
                    # started; status stays None rather than being invented.
                    "status": company.get("status"),
                    "submissionDate": company.get("submissionDate"),
                    "reminderBorderColor": row.get("reminderBorderColor"),
                }
            )
        )

    if urgent_only:
        reminders = [r for r in reminders if r.is_urgent]
    return sorted(reminders, key=lambda r: (r.remaining_days is None, r.remaining_days))
