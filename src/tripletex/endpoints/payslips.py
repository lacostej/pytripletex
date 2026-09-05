"""Payslips — what was actually paid, month by month.

**Documented and token-reachable**, unlike its neighbour: `/v2/salary/transaction`
answers **403** to the same token that reads this happily (measured 2026-09-05,
Bonita Handel). So payslips are the one headless route to a committed payroll.

This is the counterpart to `salary.py`: that module reads a run *before* it is
committed, this one reads runs that already happened. A payslip is the only
record of what an employee was actually paid — an import CSV records what was
*submitted*, and the two differ whenever anything is added inside Tripletex
afterwards (expenses, holiday money, a hand-entered correction) or whenever part
of an import silently failed to land.

**The period filter is half-open: `[from, to)`.** `monthTo` is *excluded*. One
month is `monthFrom=8, monthTo=9`; a whole year is `monthFrom=1` with
`yearTo` set to the *following* year and `monthTo=1`. Getting this wrong answers
422 with a message that names both bounds and looks like a paging complaint:

    'From and including' values (2026,7) is greater than or equal
    'To and excluding' value (2026,7) in filter.

Note the months in that message are zero-based while the parameters are
one-based, which makes it read as an off-by-one in the caller. It is not.

**A span may cross years.** 2025-06 through 2026-08 came back as one call, 411
payslips across 15 periods. `docs/api-gaps.md` §10 records this endpoint as
unable to express a full calendar year — that reading followed from assuming an
inclusive range and should be corrected: the year rolls over instead.

**`fullResultSize` is trustworthy here**, unlike `>voucherReception` and
`>nonPosted` (`api-gaps.md` §7): 24 rows and `fullResultSize=24` for one month.

Field names that are not what a caller would guess:

- the salary run is `transaction`, not `salaryTransaction`;
- net pay is `amount`; `grossAmount` is gross. There is no `netAmount`;
- `department` hangs off the payslip, not off a specification;
- `employee.employeeNumber` is a **string** — it is the same identifier Tamigo
  calls WageNumber.

All measured 2026-09-05 against Bonita Handel.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tripletex.endpoints._paging import paginate
from tripletex.models import Payslip

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

_PATH = "/v2/salary/payslip"

#: Wide enough to reconcile a payroll: who, when, and every line with its
#: amount. `salaryType.number` carries the wage type payroll work keys on.
_FIELDS = (
    "id,year,month,date,amount,grossAmount,vacationAllowanceAmount,"
    "employee(id,employeeNumber,firstName,lastName),"
    "transaction(id),department(id,name),"
    "specifications(id,salaryType(id,number,name),rate,count,amount,description)"
)


class EmptyPayslipResult(RuntimeError):
    """The endpoint answered 200 with no rows where rows were expected.

    Tripletex filters silently on insufficient permission — 200 with a reduced
    result set and no indication anything was withheld (`api-gaps.md` §2, which
    measured this very endpoint returning 0 rows on a restricted token and 89 on
    a full one). For payroll that failure mode is dangerous: an empty month and a
    month the token may not read are indistinguishable, and treating the first as
    "nothing to report" hides the second.
    """


def _next_month(year: int, month: int) -> tuple[int, int]:
    """The exclusive upper bound for an inclusive end period."""
    return (year + 1, 1) if month == 12 else (year, month + 1)


async def list_payslips(
    client: TripletexClient,
    *,
    start: tuple[int, int],
    end: tuple[int, int],
    employee_ids: list[int] | None = None,
    fields: str = _FIELDS,
) -> list[Payslip]:
    """Payslips over an **inclusive** span of `(year, month)` pairs.

    GET /v2/salary/payslip. The API's own filter is half-open; this signature is
    inclusive at both ends because that is what callers mean by "June to August",
    and the conversion is the easiest thing in the module to get wrong.

    A span may cross years — no splitting is needed. Returns whatever the
    endpoint gives, including nothing; callers that know rows should exist want
    `payslips_or_raise`.
    """
    (start_year, start_month), (end_year, end_month) = start, end
    for label, month in (("start", start_month), ("end", end_month)):
        if not 1 <= month <= 12:
            raise ValueError(f"{label} month must be 1-12, not {month}")
    if (end_year, end_month) < (start_year, start_month):
        raise ValueError(f"end {end} precedes start {start}")

    to_year, to_month = _next_month(end_year, end_month)
    params = {
        "yearFrom": str(start_year),
        "monthFrom": str(start_month),
        "yearTo": str(to_year),
        "monthTo": str(to_month),
        "fields": fields,
    }
    if employee_ids:
        params["employeeIds"] = ",".join(str(i) for i in employee_ids)

    values = await paginate(client, _PATH, params=params)
    payslips = [Payslip.model_validate(v) for v in values]
    payslips.sort(key=lambda p: (p.year or 0, p.month or 0, p.employee_number or 0))
    return payslips


async def payslips_or_raise(
    client: TripletexClient,
    *,
    start: tuple[int, int],
    end: tuple[int, int],
    employee_ids: list[int] | None = None,
) -> list[Payslip]:
    """`list_payslips`, refusing a silently empty answer.

    Use this whenever the span is expected to contain payrolls. A filtered
    result and a quiet period look identical over HTTP, and for payroll the
    difference matters.
    """
    payslips = await list_payslips(
        client, start=start, end=end, employee_ids=employee_ids
    )
    if not payslips:
        raise EmptyPayslipResult(
            f"no payslips for {start[0]}-{start[1]:02d}..{end[0]}-{end[1]:02d}. "
            "Either the span is genuinely empty or the token may not read "
            "payslips — this endpoint returns 200 for both. Use list_payslips "
            "if an empty answer is a real one."
        )
    return payslips
