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
