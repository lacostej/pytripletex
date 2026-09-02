"""Travel expenses — claims, and the queue of those awaiting approval.

Unlike bank payments and the voucher inbox, this queue is **documented and
reachable with API tokens**, so a headless job can watch it. Verified 2026-09-02
on both companies with the API module: token and web session return identical
counts, so no silent scope filtering is in play.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tripletex.endpoints._paging import paginate
from tripletex.models import TravelExpense

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

TRAVEL_EXPENSE = "/v2/travelExpense"

#: Values `state` accepts, from the specification's own enum.
TRAVEL_EXPENSE_STATES = (
    "ALL",
    "OPEN",
    "DELIVERED",
    "APPROVED",
    "REJECTED",
    "SALARY_PAID",
)

#: Submitted by the employee and not yet approved — the approval queue.
AWAITING_APPROVAL = "DELIVERED"

_FIELDS = (
    "id,number,title,date,completedDate,approvedDate,amount,paymentAmount,"
    "state,stateName,isApproved,isCompleted,attachmentCount,rejectedComment,"
    "employee(id,firstName,lastName),approvedBy(id,firstName,lastName),"
    "department(id,name),project(id,name),voucher(id)"
)


def validate_state(state: str) -> str:
    """Reject an unknown state before spending a request on a 400."""
    if state not in TRAVEL_EXPENSE_STATES:
        raise ValueError(
            f"Unknown travel expense state {state!r}. "
            f"Valid values: {', '.join(TRAVEL_EXPENSE_STATES)}"
        )
    return state


async def list_travel_expenses(
    client: TripletexClient,
    state: str = AWAITING_APPROVAL,
    employee_id: int | None = None,
    limit: int | None = None,
) -> list[TravelExpense]:
    """List expense claims in `state`, awaiting approval by default.

    GET /v2/travelExpense?state=…  — documented, and works with API tokens.

    Note the two dates on a claim mean different things: `date` is when the
    expense happened, `completedDate` is when it was submitted. Age a pending
    claim from the latter — see `TravelExpense.waiting_days`.
    """
    validate_state(state)
    params = {"state": state, "fields": _FIELDS}
    if employee_id is not None:
        params["employeeId"] = str(employee_id)

    return [
        TravelExpense.model_validate(row)
        for row in await paginate(client, TRAVEL_EXPENSE, params=params, limit=limit)
    ]


async def get_travel_expense(
    client: TripletexClient, expense_id: int
) -> TravelExpense:
    """Fetch one expense claim by id."""
    data = await client.get_json(
        f"{TRAVEL_EXPENSE}/{expense_id}", params={"fields": _FIELDS}
    )
    return TravelExpense.model_validate(data["value"])
