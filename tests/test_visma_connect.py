"""Tests for the two-phase Visma Connect login split."""

import httpx
import pytest
import respx

from tripletex.auth.visma_connect import (
    LoginState,
    _cookie_for_url,
    complete_login,
    start_login,
)
from tripletex.config import TripletexConfig
from tripletex.session import WebSession

# --- HTML fixtures ---

LOGIN_PAGE_HTML = """
<html><body>
<form action="/login" method="post">
  <input name="Username" value="" />
  <input name="RememberUsername" value="true" />
  <input name="__RequestVerificationToken" value="tok123" />
</form>
</body></html>
"""

PASSWORD_PAGE_HTML = """
<html><body>
<form action="/login/password" method="post">
  <input name="Password" value="" />
  <input name="__RequestVerificationToken" value="tok456" />
</form>
</body></html>
"""

MFA_PAGE_HTML = """
<html><body>
<form action="/login/totp" method="post">
  <input name="Totp" value="" />
  <input name="__RequestVerificationToken" value="tok789" />
</form>
</body></html>
"""

# This HTML has a JS redirect to a tripletex URL with contextId
JS_REDIRECT_HTML = """
<html><head><script>
window.location.href=decodeURIComponent('https%3A%2F%2Ftripletex.no%2Fexecute%2Fviewer%3FcontextId%3D12345')
</script></head><body></body></html>
"""

TRIPLETEX_FINAL_HTML = """
<html><head>
<script>window.CSRFToken = "abc123def456";</script>
</head><body>Dashboard</body></html>
"""

BASE_URL = "https://tripletex.no"
VISMA_URL = "https://connect.visma.com"


def _config(**overrides):
    return TripletexConfig(
        username="test@example.com",
        password_visma="secret",
        base_url=BASE_URL,
        **overrides,
    )


@respx.mock
async def test_start_login_returns_login_state_when_mfa_required():
    """start_login should return LoginState when MFA form is detected."""
    # Step 1: redirect to Visma Connect
    respx.get(f"{BASE_URL}/execute/login").mock(
        return_value=httpx.Response(302, headers={"location": f"{VISMA_URL}/login-page"})
    )
    respx.get(f"{VISMA_URL}/login-page").mock(
        return_value=httpx.Response(200, text=LOGIN_PAGE_HTML)
    )
    # Step 2: email submission
    respx.post(f"{VISMA_URL}/login").mock(
        return_value=httpx.Response(200, text=PASSWORD_PAGE_HTML)
    )
    # Step 3: password submission -> MFA page
    respx.post(f"{VISMA_URL}/login/password").mock(
        return_value=httpx.Response(200, text=MFA_PAGE_HTML)
    )

    result = await start_login(_config())

    assert isinstance(result, LoginState)
    assert result.mfa_field_name == "Totp"
    assert result.mfa_form_action == "/login/totp"
    assert result.base_url == BASE_URL
    assert isinstance(result.cookies, httpx.Cookies)


@respx.mock
async def test_start_login_returns_session_when_no_mfa():
    """start_login should return WebSession directly when no MFA is needed."""
    respx.get(f"{BASE_URL}/execute/login").mock(
        return_value=httpx.Response(302, headers={"location": f"{VISMA_URL}/login-page"})
    )
    respx.get(f"{VISMA_URL}/login-page").mock(
        return_value=httpx.Response(200, text=LOGIN_PAGE_HTML)
    )
    respx.post(f"{VISMA_URL}/login").mock(
        return_value=httpx.Response(200, text=PASSWORD_PAGE_HTML)
    )
    # Password submission returns a page with JS redirect (no MFA)
    respx.post(f"{VISMA_URL}/login/password").mock(
        return_value=httpx.Response(200, text=JS_REDIRECT_HTML)
    )
    # JS redirect lands on Tripletex (contextId in query string)
    respx.get(url__startswith=f"{BASE_URL}/execute/viewer").mock(
        return_value=httpx.Response(200, text=TRIPLETEX_FINAL_HTML)
    )

    result = await start_login(_config())

    assert isinstance(result, WebSession)
    assert result.context_id == "12345"
    headers = result.request_headers(f"{BASE_URL}/execute/viewer")
    assert headers["x-tlx-csrf-token"] == "abc123def456"


@respx.mock
async def test_complete_login_submits_mfa():
    """complete_login should submit MFA and return WebSession."""
    cookies = httpx.Cookies()
    cookies.set("session", "abc", domain="connect.visma.com", path="/")
    state = LoginState(
        cookies=cookies,
        visma_base=f"{VISMA_URL}/",
        mfa_form_action="/login/totp",
        mfa_form_data={"Totp": "", "__RequestVerificationToken": "tok789"},
        mfa_field_name="Totp",
        base_url=BASE_URL,
    )

    # MFA submission → JS redirect page
    respx.post(f"{VISMA_URL}/login/totp").mock(
        return_value=httpx.Response(200, text=JS_REDIRECT_HTML)
    )
    # JS redirect lands on Tripletex (contextId in query string)
    respx.get(url__startswith=f"{BASE_URL}/execute/viewer").mock(
        return_value=httpx.Response(200, text=TRIPLETEX_FINAL_HTML)
    )

    session = await complete_login(state, "123456")

    assert isinstance(session, WebSession)
    assert session.context_id == "12345"
    headers = session.request_headers(f"{BASE_URL}/execute/viewer")
    assert headers["x-tlx-csrf-token"] == "abc123def456"


def test_cookie_for_url_picks_the_domain_scoped_cookie():
    """When multiple cookies share a name across domains, _cookie_for_url
    should return the one scoped to the requested URL's domain."""
    cookies = httpx.Cookies()
    cookies.set("CSRFTokenWriteOnly", "tripletex-value", domain="tripletex.no", path="/")
    cookies.set("CSRFTokenWriteOnly", "visma-value", domain="connect.visma.com", path="/")

    assert _cookie_for_url(cookies, "https://tripletex.no/execute/viewer", "CSRFTokenWriteOnly") == "tripletex-value"
    assert _cookie_for_url(cookies, "https://connect.visma.com/anything", "CSRFTokenWriteOnly") == "visma-value"


def test_cookie_for_url_returns_empty_when_no_match():
    cookies = httpx.Cookies()
    cookies.set("JSESSIONID", "abc", domain="tripletex.no", path="/")
    assert _cookie_for_url(cookies, "https://tripletex.no/", "Missing") == ""


def test_websession_roundtrip_preserves_domain_scoped_cookies(tmp_path):
    """WebSession.save/load should round-trip cookies without collapsing
    same-named cookies set on different domains (regression: CookieConflict
    when multiple CSRFTokenWriteOnly cookies coexist)."""
    cookies = httpx.Cookies()
    cookies.set("CSRFTokenWriteOnly", "tlx-value", domain="tripletex.no", path="/")
    cookies.set("CSRFTokenWriteOnly", "visma-value", domain="connect.visma.com", path="/")

    session = WebSession(cookies=cookies, context_id="12345")
    path = tmp_path / "session.json"
    session.save(path)
    loaded = WebSession.load(path)

    assert loaded is not None
    values = sorted(
        c.value for c in loaded.cookies.jar if c.name == "CSRFTokenWriteOnly"
    )
    assert values == ["tlx-value", "visma-value"]
