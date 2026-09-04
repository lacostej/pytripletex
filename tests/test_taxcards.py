"""Tax cards: reading them, and the guard rails on ordering them.

Payloads captured from the Tripletex UI 2026-09-04 and verified against the
live API the same day.

The write tests matter more than the read ones. `prepare_taxcards` places a real,
irreversible bulk order against Altinn, so what is pinned here is that it cannot
be called meaningfully by accident and that it sends exactly the shape the UI
sends — not a shape we invented.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints import taxcards as tc
from tripletex.session import ApiSession

BASE_URL = "https://tripletex.no"

OK_CARD = {
    "id": 26809059, "status": "skattekortopplysningerOK",
    # Tripletex really does describe a healthy card this way.
    "statusDescription": "det har oppstått en ukjent feil.",
    "additionalInfo": "", "yearOfIncome": 2026, "utstedtDato": "2026-05-02",
    "orderId": 2143576, "arbeidstakerIdentifikator": "01019012345",
    "advanceTaxcards": [
        {"trekkode": "loennFraHovedarbeidsgiver",
         "trekkodeDescription": "Lønn fra hovedarbeidsgiver",
         "type": 2, "typeDescription": "Prosentkort",
         "tabelltype": "", "tabellnummer": "", "prosentsats": 10.0,
         "antallMndForTrekk": 0.0, "frikortbelop": 0.0,
         "remainingFreeCardAmount": 0},
    ],
}

NO_CARD_STATUS = {
    **OK_CARD, "id": 2, "status": "ikkeSkattekort",
    "statusDescription": "den ansatte har ikke skattekort. Det skal trekkes 50 % av lønn…",
}
EXPIRED_DNUMBER = {
    **OK_CARD, "id": 3,
    "status": "utgaattDnummerSkattekortForFoedselsnummerErLevert",
    "statusDescription": "den ansattes D-nummer er utløpt…",
}
KILDESKATT = {**OK_CARD, "id": 4, "additionalInfo": "kildeskattPaaLoenn"}


def _client(handler) -> TripletexClient:
    client = TripletexClient(TripletexConfig(base_url=BASE_URL))
    client._session = ApiSession(session_token="tok", company_id=0)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )
    return client


def _rows(*employees):
    def handler(request: httpx.Request) -> httpx.Response:
        # The endpoint ignores paging: whole set, fullResultSize always 0.
        return httpx.Response(
            200,
            json={"values": list(employees), "from": 0,
                  "count": len(employees), "fullResultSize": 0},
        )

    return handler


def _employee(number, name, card):
    return {"id": 100 + number, "displayName": name,
            "number": str(number), "taxcard": card}


class TestReading:
    async def test_parses_a_card_and_its_deduction_rule(self):
        (e,) = await tc.list_taxcards(
            _client(_rows(_employee(139, "Alexander Kalseth", OK_CARD))), 2026
        )

        assert e.has_card and e.taxcard.is_ok
        assert e.taxcard.issued_date == datetime.date(2026, 5, 2)
        assert e.taxcard.order_id == 2143576
        (rule,) = e.taxcard.advance_taxcards
        assert rule.type_description == "Prosentkort"
        assert rule.prosentsats == Decimal("10")

    async def test_filters_are_year_hasQuit_query(self):
        """Not taxcardYear/yearOfIncome, which 422 and read as unavailable."""
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await tc.list_taxcards(_client(handler), 2026, include_quit=True, query="ale")

        assert seen[0].params["year"] == "2026"
        assert seen[0].params["hasQuit"] == "true"
        assert seen[0].params["query"] == "ale"
        assert "taxcardYear" not in seen[0].params

    async def test_include_quit_defaults_off(self):
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json={"values": [], "fullResultSize": 0})

        await tc.list_taxcards(_client(handler), 2026)

        assert seen[0].params["hasQuit"] == "false"

    async def test_whole_set_returned_despite_fullResultSize_zero(self):
        """Paging does not apply here; counting rows is the only way."""
        rows = [_employee(n, f"E{n}", OK_CARD) for n in range(30)]
        got = await tc.list_taxcards(_client(_rows(*rows)), 2026)

        assert len(got) == 30


class TestIssueDetection:
    async def test_healthy_card_is_not_an_issue_despite_its_description(self):
        """The trap: `statusDescription` says "an unknown error occurred" on
        every good card. Keying on it reports the whole payroll as broken."""
        (e,) = await tc.list_taxcards(
            _client(_rows(_employee(1, "Fine", OK_CARD))), 2026
        )

        assert "ukjent feil" in e.taxcard.status_description
        assert e.issue is None

    async def test_missing_card_object_is_an_issue(self):
        (e,) = await tc.list_taxcards(
            _client(_rows(_employee(2, "Never ordered", None))), 2026
        )

        assert not e.has_card
        assert "ordered or returned" in e.issue

    async def test_no_taxcard_status_is_an_issue(self):
        issues = await tc.taxcard_issues(
            _client(_rows(_employee(83, "Cecilia", NO_CARD_STATUS))), 2026
        )

        assert issues[0].issue == "ikkeSkattekort"

    async def test_expired_dnumber_is_an_issue(self):
        """The personal-number-changed case: a card under the employee's
        fødselsnummer is waiting to be fetched."""
        issues = await tc.taxcard_issues(
            _client(_rows(_employee(72, "Lorena", EXPIRED_DNUMBER))), 2026
        )

        assert issues[0].issue.startswith("utgaattDnummer")

    async def test_kildeskatt_is_a_note_not_an_issue(self):
        """`additionalInfo` is set while the status stays OK — payroll needs to
        know, but nothing is broken."""
        (e,) = await tc.list_taxcards(
            _client(_rows(_employee(149, "Cornelia", KILDESKATT))), 2026
        )

        assert e.issue is None
        assert e.note == "kildeskattPaaLoenn"

    async def test_issues_excludes_the_healthy_majority(self):
        rows = [
            _employee(1, "Fine", OK_CARD),
            _employee(2, "Note only", KILDESKATT),
            _employee(83, "No card", NO_CARD_STATUS),
            _employee(9, "Never ordered", None),
        ]
        issues = await tc.taxcard_issues(_client(_rows(*rows)), 2026)

        assert [e.display_name for e in issues] == ["No card", "Never ordered"]


class TestAltinnStatus:
    def _value(self, value):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"value": value})

        return handler

    async def test_altinn_deadline_parses(self):
        when = await tc.altinn_logged_in_until(
            _client(self._value("2026-09-05 00:00:00"))
        )
        assert when == datetime.datetime(2026, 9, 5, 0, 0)

    async def test_absent_altinn_login_is_none_not_an_error(self):
        """Not signed in is a state to report, not an error to retry."""
        assert await tc.altinn_logged_in_until(_client(self._value(""))) is None

    async def test_order_id_and_progress(self):
        assert await tc.last_order_id(_client(self._value(143478))) == 143478
        assert await tc.application_in_progress(_client(self._value(True)))
        assert not await tc.application_in_progress(_client(self._value(False)))


class TestOrdering:
    """Guard rails on the irreversible calls."""

    def _capture(self, store):
        def handler(request: httpx.Request) -> httpx.Response:
            store["path"] = request.url.path
            store["body"] = request.content.decode()
            return httpx.Response(200, json={"value": True})

        return handler

    async def test_sends_exactly_the_shape_the_ui_sends(self):
        seen: dict = {}
        await tc.prepare_taxcards(
            _client(self._capture(seen)),
            employee_ids=[11558881, 6540664, 4888751],
            contact_employee_id=4229621,
            contact_email="payroll@example.com",
            year=2026,
        )

        import json
        body = json.loads(seen["body"])
        # A comma-joined string, not a list — the API's own shape.
        assert body["employeeIds"] == "11558881,6540664,4888751"
        assert body["contactEmployeeId"] == 4229621
        assert body["taxcardYear"] == 2026
        assert body["mobilePhoneCountryId"] == 161      # Norway
        assert body["notificationType"] == "1"

    async def test_empty_employee_list_refuses(self):
        seen: dict = {}
        with pytest.raises(ValueError, match="nothing to order"):
            await tc.prepare_taxcards(
                _client(self._capture(seen)),
                employee_ids=[], contact_employee_id=1,
                contact_email="a@b.c", year=2026,
            )
        assert "path" not in seen  # nothing was sent

    async def test_missing_contact_refuses(self):
        seen: dict = {}
        with pytest.raises(ValueError, match="contact_email"):
            await tc.prepare_taxcards(
                _client(self._capture(seen)),
                employee_ids=[1], contact_employee_id=1,
                contact_email="", year=2026,
            )
        assert "path" not in seen

    async def test_arguments_are_keyword_only(self):
        """So an ordering call can never be assembled from loose positionals."""
        with pytest.raises(TypeError):
            await tc.prepare_taxcards(
                _client(self._capture({})), [1], 1, "a@b.c", 2026
            )

    async def test_fetch_returns_the_async_status_string(self):
        """It answers immediately and works on server-side; -1 then 0 observed."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"value": "-1"})

        assert await tc.fetch_taxcards_from_altinn(_client(handler)) == "-1"
