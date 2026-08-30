"""Every web-only operation should fail the same way under API-token auth.

Without these guards the failures were inconsistent and ugly: a raw 403
HTTPStatusError from the endpoint, a bare RuntimeError from company_context, or
an AttributeError from reading `context_id` off an ApiSession.
"""

from pathlib import Path

import pytest

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.endpoints.employees import fetch_employee_access, fetch_employee_overview
from tripletex.endpoints.inbox import list_inbox
from tripletex.endpoints.payments import list_payments
from tripletex.endpoints.vouchers import download_voucher_document
from tripletex.endpoints.wages import (
    fetch_company_wage_settings,
    fetch_employee_list,
    fetch_employee_salary,
)
from tripletex.models import Company
from tripletex.session import ApiSession, WebSessionRequired, require_web_session


class TokenClient:
    """Stands in for a client authenticated with consumer/employee tokens."""

    session = ApiSession(session_token="t", company_id=0)

    async def get_json(self, path, params=None):  # pragma: no cover - must not run
        raise AssertionError(f"request escaped the guard: {path}")

    async def post_json(self, path, params=None, json_body=None):  # pragma: no cover
        raise AssertionError(f"request escaped the guard: {path}")

    async def get_html(self, path, params=None):  # pragma: no cover
        raise AssertionError(f"request escaped the guard: {path}")

    async def download(self, path, params, dest):  # pragma: no cover
        raise AssertionError(f"request escaped the guard: {path}")


WEB_ONLY = [
    ("payments", lambda c: list_payments(c)),
    ("inbox", lambda c: list_inbox(c)),
    ("employee overview", lambda c: fetch_employee_overview(c)),
    ("employee access", lambda c: fetch_employee_access(c, 1)),
    ("wage employee list", lambda c: fetch_employee_list(c)),
    ("employee salary page", lambda c: fetch_employee_salary(c, 1)),
    ("company wage settings", lambda c: fetch_company_wage_settings(c)),
    ("voucher document", lambda c: download_voucher_document(c, 1, Path("/tmp/x.pdf"))),
]


@pytest.mark.parametrize("name,call", WEB_ONLY, ids=[n for n, _ in WEB_ONLY])
async def test_web_only_operations_raise_before_any_request(name, call):
    with pytest.raises(WebSessionRequired) as excinfo:
        await call(TokenClient())
    # Uniform, actionable wording — this is what the CLI surfaces.
    assert "--auth web" in str(excinfo.value)


async def test_company_switching_raises_the_same_error():
    client = TripletexClient(TripletexConfig())
    client._session = ApiSession(session_token="t", company_id=0)
    with pytest.raises(WebSessionRequired) as excinfo:
        async with client.company_context(Company(id=1, displayName="Test AS")):
            pass
    assert "Switching company" in str(excinfo.value)
    assert "--auth web" in str(excinfo.value)


def test_guard_returns_the_session_when_it_is_a_web_session():
    import httpx

    from tripletex.session import WebSession

    session = WebSession(cookies=httpx.Cookies(), context_id="123")
    assert require_web_session(session, "anything") is session


async def test_expired_session_without_a_terminal_raises_instead_of_prompting(monkeypatch):
    """A scheduler can never answer an MFA prompt — fail with instructions instead."""
    import sys

    from tripletex.auth import visma_connect
    from tripletex.auth.visma_connect import LoginState, visma_connect_login
    from tripletex.session import AuthUnavailable, InteractiveLoginRequired

    state = LoginState(
        cookies=None, visma_base="https://connect.visma.com",
        mfa_form_action="/login/totp", mfa_form_data={},
        mfa_field_name="AuthCode", base_url="https://tripletex.no",
    )

    async def fake_phase1(config, http):
        return state

    monkeypatch.setattr(visma_connect, "_do_login_phase1", fake_phase1)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    def explode(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("prompted for MFA on a non-interactive stdin")

    monkeypatch.setattr(sys.stdin, "readline", explode, raising=False)

    config = TripletexConfig(username="u", password_visma="p", env_name="prod")
    with pytest.raises(InteractiveLoginRequired) as excinfo:
        await visma_connect_login(config)

    message = str(excinfo.value)
    assert "tripletex --env prod login" in message
    # The CLI catches the shared base, so both auth failures render the same way.
    assert isinstance(excinfo.value, AuthUnavailable)
