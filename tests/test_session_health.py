"""Tests for the unattended session contract.

Covers what a headless collector needs: transportable session state, a typed
answer to "is this session usable", and a typed failure when it dies mid-run.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.session import (
    ApiSession,
    AuthUnavailable,
    SessionExpired,
    SessionStatus,
    WebSession,
)

BASE_URL = "https://tripletex.no"


def _web_session(**kwargs) -> WebSession:
    cookies = httpx.Cookies()
    cookies.set("JSESSIONID", "sess123", domain="tripletex.no", path="/")
    cookies.set("CSRFTokenWriteOnly", "csrf456", domain="tripletex.no", path="/")
    return WebSession(cookies=cookies, context_id="32611682", **kwargs)


class TestTransportableState:
    """§3: session state has to move between machines as data, not as a file."""

    def test_round_trips_through_a_plain_dict(self):
        original = _web_session()
        restored = WebSession.from_dict(original.to_dict())

        assert restored is not None
        assert restored.context_id == original.context_id
        assert restored.request_headers(BASE_URL)["x-tlx-csrf-token"] == "csrf456"

    def test_serialised_form_is_json_safe(self):
        import json

        # The point of the format: it survives a network hop as data.
        payload = json.dumps(_web_session().to_dict())
        assert WebSession.from_dict(json.loads(payload)) is not None

    def test_cookie_attributes_survive(self):
        cookies = httpx.Cookies()
        response = httpx.Response(
            200,
            headers=[
                ("set-cookie", "JSESSIONID=a; Domain=tripletex.no; Path=/x; Secure; HttpOnly"),
            ],
            request=httpx.Request("GET", BASE_URL),
        )
        cookies.extract_cookies(response)
        restored = WebSession.from_dict(
            WebSession(cookies=cookies, context_id="1").to_dict()
        )

        assert restored is not None
        (cookie,) = list(restored.cookies.jar)
        assert cookie.secure is True
        assert cookie.path == "/x"
        assert "HttpOnly" in cookie._rest

    def test_the_pickle_is_gone(self):
        # Regression guard: unpickling transported state would be RCE in the
        # process holding the Tripletex credentials.
        import json

        payload = json.dumps(_web_session().to_dict())
        assert "pickle" not in payload
        assert isinstance(json.loads(payload)["cookies"], list)

    def test_version_1_pickle_format_is_refused_not_migrated(self):
        # Reading a v1 file would mean pickle.loads on it — the thing removed.
        v1 = {"type": "web", "context_id": "1", "cookies": "gASVEgAAAAAAAABd"}
        assert WebSession.from_dict(v1) is None

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "not a dict",
            {"version": 2, "type": "api", "context_id": "1", "cookies": []},
            {"version": 2, "type": "web", "cookies": []},
            {"version": 2, "type": "web", "context_id": "1", "cookies": "nope"},
            {"version": 2, "type": "web", "context_id": "1", "cookies": [{"bogus": 1}]},
        ],
    )
    def test_malformed_payloads_return_none(self, payload):
        assert WebSession.from_dict(payload) is None

    def test_save_and_load_still_work(self, tmp_path: Path):
        path = tmp_path / "session.json"
        _web_session().save(path)

        loaded = WebSession.load(path)
        assert loaded is not None
        assert loaded.context_id == "32611682"


class TestSessionAge:
    """§4: the only way anyone learns the real web-session TTL."""

    def test_created_at_defaults_to_now(self):
        assert _web_session().age < timedelta(seconds=5)

    def test_created_at_survives_a_round_trip(self):
        established = datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)
        restored = WebSession.from_dict(
            _web_session(created_at=established).to_dict()
        )

        assert restored is not None
        assert restored.created_at == established
        assert restored.age > timedelta(days=1)

    def test_missing_timestamp_does_not_break_older_state(self):
        payload = _web_session().to_dict()
        del payload["created_at"]

        restored = WebSession.from_dict(payload)
        assert restored is not None
        assert restored.age < timedelta(seconds=5)

    def test_naive_timestamp_is_assumed_utc(self):
        # A naive value must not make `age` raise on mixed-awareness subtraction.
        payload = _web_session().to_dict()
        payload["created_at"] = "2026-08-20T09:30:00"

        restored = WebSession.from_dict(payload)
        assert restored is not None
        assert restored.age > timedelta(days=1)


class _ProbeClient(TripletexClient):
    """Client whose probe response is canned, so no network is needed."""

    def __init__(self, config, session, status_code=200, error=None):
        super().__init__(config)
        self._session = session
        self._status_code = status_code
        self._error = error

    async def get_json(self, path, params=None):
        if self._error is not None:
            raise self._error
        if self._status_code == 401:
            raise SessionExpired(path, self.config.env_name)
        if self._status_code != 200:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", BASE_URL),
                response=httpx.Response(self._status_code),
            )
        return {"values": []}


class TestSessionStatus:
    """§1: a public health check that never blocks on stdin."""

    async def test_working_session_is_valid(self):
        client = _ProbeClient(TripletexConfig(), _web_session())
        assert await client.session_status() is SessionStatus.VALID

    async def test_rejected_session_is_expired(self):
        client = _ProbeClient(TripletexConfig(), _web_session(), status_code=401)
        assert await client.session_status() is SessionStatus.EXPIRED

    async def test_no_stored_session_needs_interactive_login(self, tmp_path: Path):
        config = TripletexConfig(session_dir=tmp_path, env_name="nothing-here")
        client = _ProbeClient(config, None)
        assert await client.session_status() is SessionStatus.NEEDS_INTERACTIVE_LOGIN

    async def test_transient_network_failure_is_raised_not_reported(self):
        """The distinction the whole ask exists for: dead session vs bad day."""
        client = _ProbeClient(
            TripletexConfig(),
            _web_session(),
            error=httpx.ConnectError("connection reset"),
        )
        with pytest.raises(httpx.RequestError):
            await client.session_status()

    async def test_unexpected_status_is_raised_not_reported(self):
        client = _ProbeClient(TripletexConfig(), _web_session(), status_code=500)
        with pytest.raises(httpx.HTTPStatusError):
            await client.session_status()

    async def test_probe_never_reads_stdin(self, monkeypatch):
        import sys

        def explode(*a, **kw):  # pragma: no cover - must not be reached
            raise AssertionError("session_status() blocked on stdin")

        monkeypatch.setattr(sys.stdin, "readline", explode, raising=False)
        client = _ProbeClient(TripletexConfig(), _web_session(), status_code=401)
        assert await client.session_status() is SessionStatus.EXPIRED


class TestMidRunExpiry:
    """§2: a session that dies during a run must not look like any other 500."""

    async def test_401_on_a_web_session_raises_a_typed_error(self):
        config = TripletexConfig(base_url=BASE_URL, env_name="prod")
        client = TripletexClient(config)
        client._session = _web_session()

        async def fake_request(method, path, **kwargs):
            return httpx.Response(
                401, request=httpx.Request(method, f"{BASE_URL}{path}")
            )

        client._http = httpx.AsyncClient(base_url=BASE_URL)
        client._http.request = fake_request

        with pytest.raises(SessionExpired) as excinfo:
            await client.get_json("/v2/bank/payment")

        # The CLI catches the shared base, so auth failures render alike.
        assert isinstance(excinfo.value, AuthUnavailable)
        assert "tripletex --env prod login" in str(excinfo.value)

    async def test_api_sessions_keep_the_plain_http_error(self):
        """Token auth has no interactive login to point a caller at."""
        config = TripletexConfig(base_url=BASE_URL)
        client = TripletexClient(config)
        client._session = ApiSession(session_token="tok", company_id=0)

        async def fake_request(method, path, **kwargs):
            return httpx.Response(
                401, request=httpx.Request(method, f"{BASE_URL}{path}")
            )

        client._http = httpx.AsyncClient(base_url=BASE_URL)
        client._http.request = fake_request

        with pytest.raises(httpx.HTTPStatusError):
            await client.get_json("/v2/ledger/voucher")

    async def test_other_failures_stay_untyped(self):
        """A 500 is a bad day, not a dead session."""
        config = TripletexConfig(base_url=BASE_URL)
        client = TripletexClient(config)
        client._session = _web_session()

        async def fake_request(method, path, **kwargs):
            return httpx.Response(
                503, request=httpx.Request(method, f"{BASE_URL}{path}")
            )

        client._http = httpx.AsyncClient(base_url=BASE_URL)
        client._http.request = fake_request

        with pytest.raises(httpx.HTTPStatusError):
            await client.get_json("/v2/bank/payment")
