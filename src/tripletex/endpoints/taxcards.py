"""Tax cards (skattekort) — reading them, and ordering them from Altinn.

**Undocumented but token-reachable.** None of these paths appear in the official
specification, yet the specification publishes their *models*
(`SalaryTaxcardInternal`, `TaxcardEmployeeInternal`, `PrepareTaxcardsArgsInternal`)
— Tripletex shipped the schemas and withheld the routes. They answer to an API
token rather than 403-ing like `/v2/tripletexDashboard/*`, so this whole module
works headlessly. Captured from the UI and verified 2026-09-04.

Ordering is a legal obligation for a Norwegian employer, which is why the write
calls are here rather than left out. They are still the sharpest things in this
library: `prepare_taxcards` places a bulk order for named employees against
Altinn, and it is not reversible. Both take keyword-only arguments with no
defaults for anything that matters, so neither can be invoked meaningfully by
accident.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from tripletex.models import TaxcardEmployee

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

_BASE = "/v2/salary/tskinternal/taxcard"

_FIELDS = "id,displayName,number,taxcard(*,advanceTaxcards(*))"


def _timestamp(raw: object) -> datetime.datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


async def list_taxcards(
    client: TripletexClient,
    year: int,
    former_employees: bool = False,
    query: str = "",
) -> list[TaxcardEmployee]:
    """Employees and their tax cards for `year`.

    GET /v2/salary/tskinternal/taxcard. The filters are `year`, `hasQuit` and
    `query` — not the `taxcardYear`/`yearOfIncome` the published schema names
    suggest, which answer 422 and read like the endpoint is unavailable.

    **`hasQuit` selects, it does not widen.** `false` returns current
    employees and `true` returns *only* former ones — the two sets are nearly
    disjoint. Measured: Handel 30 and 46 with one person in both, Services 15
    and 13 with four. The overlap is people holding an ended employment and a
    current one, which happens whenever someone's unit changes. Calling the
    parameter "include quit" was wrong and produced a tidy, confident,
    incorrect reading of who was affected — use `all_taxcards` for everyone.

    **Paging does not work here and must not be inferred.** `count=0` returns
    the whole set and `fullResultSize` comes back `0` regardless — the third
    paging behaviour in this API, alongside ordinary paging and the
    `>nonPosted` shape. Counting rows is the only way to know how many there
    are.
    """
    data = await client.get_json(
        _BASE,
        params={
            "count": "0",
            "from": "0",
            "year": str(year),
            "query": query,
            "hasQuit": "true" if former_employees else "false",
            "fields": _FIELDS,
        },
    )
    return [TaxcardEmployee.model_validate(v) for v in data.get("values", [])]


async def all_taxcards(
    client: TripletexClient, year: int, query: str = ""
) -> list[TaxcardEmployee]:
    """Current *and* former employees, deduplicated — two calls, one list.

    Needed because `hasQuit` selects rather than widens, so neither value alone
    answers "everyone". Someone appearing in both sets is returned once.
    """
    current = await list_taxcards(client, year, False, query)
    former = await list_taxcards(client, year, True, query)
    merged = {e.id: e for e in current}
    for e in former:
        merged.setdefault(e.id, e)
    return list(merged.values())


async def taxcard_issues(
    client: TripletexClient,
    year: int,
    scope: str = "current",
) -> list[TaxcardEmployee]:
    """Only the employees whose tax card needs a human.

    `scope` is `"current"`, `"former"` or `"all"`. It defaults to current
    employees, because that is who you are about to pay — and because the
    distinction is not cosmetic: on 2026 every outstanding issue across both
    companies belongs to someone who has *left*, while current staff are clean.
    A check that quietly mixed the two would raise alarms about people nobody
    is paying.

    Covers both failure shapes: a card returned with a problem, and no card at
    all. Measured on 2026 — `utgaattDnummerSkattekortForFoedselsnummerErLevert`
    (the employee's D-number expired and a card under their fødselsnummer is
    waiting), `ikkeSkattekort` (no card, so 50% must be withheld), and seven
    people with no card object whatsoever.

    A `kildeskattPaaLoenn` note is *not* an issue and is excluded — the status
    stays OK. Read `TaxcardEmployee.note` for those.
    """
    if scope == "all":
        rows = await all_taxcards(client, year)
    elif scope == "former":
        rows = await list_taxcards(client, year, former_employees=True)
    elif scope == "current":
        rows = await list_taxcards(client, year, former_employees=False)
    else:
        raise ValueError(f"scope must be 'current', 'former' or 'all', not {scope!r}")
    return [e for e in rows if e.issue]


async def altinn_logged_in_until(client: TripletexClient) -> datetime.datetime | None:
    """When the Altinn login backing tax card fetches expires.

    GET .../isLoggedInAltinnUntil. Ordering needs a live Altinn session, so a
    scheduled fetch has to treat "not logged in" as a real state to report
    rather than an error to retry — no amount of retrying signs in to Altinn.
    """
    data = await client.get_json(f"{_BASE}/isLoggedInAltinnUntil")
    return _timestamp(data.get("value"))


async def last_updated(client: TripletexClient) -> datetime.datetime | None:
    """When tax cards were last refreshed from Altinn."""
    data = await client.get_json(f"{_BASE}/lastUpdatedDate")
    return _timestamp(data.get("value"))


async def last_order_id(client: TripletexClient) -> int | None:
    """The most recent Altinn order id. Increments with each order placed."""
    value = (await client.get_json(f"{_BASE}/lastUpdatedOrderId")).get("value")
    return value if isinstance(value, int) else None


async def application_in_progress(client: TripletexClient) -> bool:
    """Whether an order is still being processed.

    GET .../checkTaxcardApplicationStatus. `fetch_taxcards_from_altinn` returns
    immediately and the work continues server-side, so this is how a caller
    knows to wait before reading the cards back.
    """
    return bool((await client.get_json(f"{_BASE}/checkTaxcardApplicationStatus")).get("value"))


async def uses_altinn3(client: TripletexClient) -> bool:
    """Whether this company is on the Altinn 3 integration."""
    return bool((await client.get_json(f"{_BASE}/useAltinn3")).get("value"))


async def prepare_taxcards(
    client: TripletexClient,
    *,
    employee_ids: list[int],
    contact_employee_id: int,
    contact_email: str,
    year: int,
    mobile_phone: str = "",
    mobile_phone_country_id: int = 161,
    notification_type: str = "1",
) -> bool:
    """Register an order for `employee_ids`' tax cards. **Places a real order.**

    POST .../prepareTaxcards, then call `fetch_taxcards_from_altinn`. Both are
    needed: this stages the request, that one runs it.

    Not reversible, and it increments the company's Altinn order id. The
    notification goes to `contact_email` — the person running the order, in
    every capture seen — so nobody else is contacted by Tripletex; whether
    Skatteetaten logs the access for the employees themselves is outside this
    API.

    Arguments are keyword-only and the ones that matter have no defaults, so
    this cannot be called meaningfully without stating who is being ordered for
    and who is accountable. `mobile_phone_country_id` 161 is Norway.
    """
    if not employee_ids:
        raise ValueError("employee_ids is empty — nothing to order")
    if not contact_email:
        raise ValueError("contact_email is required; it receives the notification")

    logger.warning(
        "Ordering tax cards from Altinn for %d employee(s), year %d, notifying %s",
        len(employee_ids), year, contact_email,
    )
    data = await client.post_json(
        f"{_BASE}/prepareTaxcards",
        json_body={
            # A comma-joined string, not a list — the API's own shape.
            "employeeIds": ",".join(str(i) for i in employee_ids),
            "contactEmployeeId": contact_employee_id,
            "contactEmail": contact_email,
            "taxcardYear": year,
            "mobilePhone": mobile_phone,
            "mobilePhoneCountryId": mobile_phone_country_id,
            "notificationType": notification_type,
        },
    )
    return bool(data.get("value"))


async def fetch_taxcards_from_altinn(client: TripletexClient) -> str | None:
    """Run the order staged by `prepare_taxcards`. **Contacts Altinn.**

    POST .../fetchTaxcardsFromAltinn, no body.

    **Asynchronous.** It answers immediately with a status string — `"-1"` then
    `"0"` across two observed calls — while the work continues server-side. Poll
    `application_in_progress` until it is false, and watch `last_order_id`
    change, before reading cards back with `list_taxcards`. Reading straight
    after this returns the *previous* state.
    """
    logger.warning("Fetching tax cards from Altinn")
    value = (await client.post_json(f"{_BASE}/fetchTaxcardsFromAltinn", json_body={})).get("value")
    return value if isinstance(value, str) else None
