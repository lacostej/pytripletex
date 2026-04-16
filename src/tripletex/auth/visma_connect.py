"""Automated Visma Connect login flow.

The current flow (2026):
1. GET tripletex.no/execute/login → redirect chain to connect.visma.com
2. Submit email (POST / with Username field)
3. Submit password (POST /login/password with Password field)
4. Submit MFA code (POST /login/totp or similar)
5. Follow redirects back to tripletex.no
6. Extract contextId from final URL

Supports two usage modes:
- **CLI (one-shot):** `visma_connect_login(config)` — prompts for MFA on stdin
- **Web (two-phase):** `start_login()` → `LoginState` → `complete_login(state, code)`
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse
from urllib.request import Request

import httpx
from bs4 import BeautifulSoup

from tripletex.parsers.js import extract_csrf_token, extract_js_redirect_url
from tripletex.session import WebSession

if TYPE_CHECKING:
    from tripletex.config import TripletexConfig

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/116.0"


@dataclass
class LoginState:
    """Intermediate state between email/password and MFA submission.

    In-memory only — not serialized. ``cookies`` is the live httpx jar so
    domain/path scoping is preserved across the MFA submission.
    """

    cookies: httpx.Cookies
    visma_base: str
    mfa_form_action: str
    mfa_form_data: dict[str, str]
    mfa_field_name: str  # "AuthCode" or "Totp"
    base_url: str  # Tripletex base URL, needed to complete login


def _resolve_url(location: str, response_url: str) -> str:
    """Resolve a redirect location against the request URL."""
    if location.startswith("http"):
        return location
    parsed = urlparse(response_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if location.startswith("/"):
        return base + location
    return urljoin(response_url, location)


def _get_forms(html: str) -> list[tuple[str, str, dict[str, str]]]:
    """Extract all forms from HTML. Returns list of (action, method, {name: value})."""
    soup = BeautifulSoup(html, "lxml")
    forms = []
    for form in soup.find_all("form"):
        action = form.get("action", "")
        method = form.get("method", "get").lower()
        data: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                data[name] = inp.get("value", "")
        forms.append((action, method, data))
    return forms


async def start_login(
    config: TripletexConfig,
    http: httpx.AsyncClient | None = None,
) -> WebSession | LoginState:
    """Run the email + password steps of Visma Connect login.

    Returns:
        WebSession — if no MFA is required (login complete)
        LoginState — if MFA is required (call complete_login next)
    """
    if not config.username:
        raise ValueError("username required for Visma Connect login")
    if not config.password_visma:
        raise ValueError("password_visma required for Visma Connect login")

    own_client = http is None
    if http is None:
        http = httpx.AsyncClient(timeout=30.0)

    try:
        return await _do_login_phase1(config, http)
    finally:
        if own_client:
            await http.aclose()


async def complete_login(
    state: LoginState,
    mfa_code: str,
    http: httpx.AsyncClient | None = None,
) -> WebSession:
    """Submit MFA code and complete the Visma Connect login.

    Args:
        state: LoginState returned by start_login
        mfa_code: The 6-digit MFA code
        http: Optional httpx client (created if not provided)

    Returns:
        WebSession ready for Tripletex API calls
    """
    own_client = http is None
    if http is None:
        http = httpx.AsyncClient(timeout=30.0)

    try:
        cookies = state.cookies

        # Submit MFA form
        data = dict(state.mfa_form_data)
        data[state.mfa_field_name] = mfa_code

        form_url = _resolve_url(state.mfa_form_action, state.visma_base)
        resp = await http.post(
            form_url,
            data=data,
            headers={"User-Agent": _UA},
            cookies=cookies,
            follow_redirects=True,
        )
        _collect_cookies(cookies, resp)

        return await _finish_login(resp, cookies, state.base_url, http)
    finally:
        if own_client:
            await http.aclose()


async def visma_connect_login(
    config: TripletexConfig,
    http: httpx.AsyncClient | None = None,
) -> WebSession:
    """Perform the full Visma Connect login flow (CLI — prompts for MFA on stdin)."""
    if not config.username:
        raise ValueError("username required for Visma Connect login")
    if not config.password_visma:
        raise ValueError("password_visma required for Visma Connect login")

    own_client = http is None
    if http is None:
        http = httpx.AsyncClient(timeout=30.0)

    try:
        result = await _do_login_phase1(config, http)

        if isinstance(result, WebSession):
            return result

        # MFA required — prompt on stdin (CLI mode)
        print("Enter your 6-digit MFA code: ", end="", flush=True, file=sys.stderr)
        auth_code = sys.stdin.readline().strip()

        return await complete_login(result, auth_code, http)
    finally:
        if own_client:
            await http.aclose()


async def _do_login_phase1(
    config: TripletexConfig,
    http: httpx.AsyncClient,
) -> WebSession | LoginState:
    """Email + password steps. Returns WebSession or LoginState (if MFA needed)."""
    # Step 1: Follow redirect chain from Tripletex to Visma Connect login page
    url = f"{config.base_url}/execute/login"
    cookies = httpx.Cookies()

    resp = await _follow_redirects(http, url, cookies)
    visma_base = _resolve_url("/", str(resp.url))

    # Step 2: Submit email
    forms = _get_forms(resp.text)
    email_form = _find_form_with_field(forms, "Username")
    if not email_form:
        raise RuntimeError("Could not find email form on Visma Connect page")

    action, _, data = email_form
    data["Username"] = config.username
    data.pop("RememberUsername", None)

    form_url = _resolve_url(action, visma_base)
    print(f"Submitting email to Visma Connect...", file=sys.stderr)

    resp = await http.post(
        form_url,
        data=data,
        headers={"User-Agent": _UA},
        cookies=cookies,
        follow_redirects=True,
    )
    _collect_cookies(cookies, resp)

    # Step 3: Submit password
    forms = _get_forms(resp.text)
    password_form = _find_form_with_field(forms, "Password")
    if not password_form:
        raise RuntimeError(
            f"Could not find password form. Page URL: {resp.url}\n"
            f"Forms found: {[(a, list(d.keys())) for a, _, d in forms]}"
        )

    action, _, data = password_form
    data["Password"] = config.password_visma

    form_url = _resolve_url(action, visma_base)
    print("Submitting password...", file=sys.stderr)

    resp = await http.post(
        form_url,
        data=data,
        headers={"User-Agent": _UA},
        cookies=cookies,
        follow_redirects=True,
    )
    _collect_cookies(cookies, resp)

    # Step 4: Check if MFA is required
    forms = _get_forms(resp.text)
    mfa_form = _find_form_with_field(forms, "AuthCode") or _find_form_with_field(forms, "Totp")

    if mfa_form:
        action, _, data = mfa_form
        mfa_field = "AuthCode" if "AuthCode" in data else "Totp"
        return LoginState(
            cookies=cookies,
            visma_base=visma_base,
            mfa_form_action=action,
            mfa_form_data=data,
            mfa_field_name=mfa_field,
            base_url=config.base_url,
        )

    # No MFA — finish login directly
    return await _finish_login(resp, cookies, config.base_url, http)


async def _finish_login(
    resp: httpx.Response,
    cookies: httpx.Cookies,
    base_url: str,
    http: httpx.AsyncClient,
) -> WebSession:
    """Follow post-auth redirects, extract contextId and CSRF token."""
    # Step 5: Follow redirects back to Tripletex
    final_url = str(resp.url)
    max_redirects = 10

    # Match against configured base URL domain (supports test envs like tripletex.is)
    base_domain = urlparse(base_url).netloc

    for _ in range(max_redirects):
        if base_domain in final_url and "contextId" in final_url:
            break

        # Check for JS redirect
        js_redirect = extract_js_redirect_url(resp.text)
        if js_redirect:
            final_url = _resolve_url(js_redirect, final_url)
            resp = await http.get(
                final_url,
                headers={"User-Agent": _UA},
                cookies=cookies,
                follow_redirects=True,
            )
            _collect_cookies(cookies, resp)
            final_url = str(resp.url)
            continue

        # Check for auto-submit forms (common in OAuth flows)
        forms = _get_forms(resp.text)
        if forms and len(forms) == 1:
            action, method, data = forms[0]
            if method == "post" and not _is_login_form(data):
                form_url = _resolve_url(action, final_url)
                resp = await http.post(
                    form_url,
                    data=data,
                    headers={"User-Agent": _UA},
                    cookies=cookies,
                    follow_redirects=True,
                )
                _collect_cookies(cookies, resp)
                final_url = str(resp.url)
                continue

        break

    # Step 6: Extract contextId
    context_match = re.search(r"contextId=(\d+)", final_url)
    if not context_match:
        context_match = re.search(r"contextId=(\d+)", resp.text)
    if not context_match:
        # Detect common failure: bounced back to Visma login/password page
        parsed_final = urlparse(final_url)
        if "connect.visma.com" in parsed_final.netloc and parsed_final.path in (
            "/password",
            "/login/password",
            "/",
            "/login",
        ):
            raise RuntimeError(
                "MFA verification failed — ended up back on Visma login page "
                f"({final_url}). The code may have been wrong or expired, "
                "or the account requires a different authentication step."
            )
        # General failure — include forms found for diagnostics
        diag_forms = _get_forms(resp.text)
        form_summary = [
            {"action": a, "method": m, "fields": list(d.keys())}
            for a, m, d in diag_forms
        ]
        raise RuntimeError(
            f"Could not extract contextId. Final URL: {final_url}\n"
            f"Forms on page: {form_summary}\n"
            f"Response snippet: {resp.text[:500]}"
        )

    context_id = context_match.group(1)

    # Step 7: Make sure the CSRF token is in the cookie jar, scoped to the
    # Tripletex base URL domain. request_headers() will pull it from the jar
    # per-request, so the jar is the source of truth.
    #
    # The cookie is normally set by Tripletex via Set-Cookie during the login
    # redirect chain. If for some reason it isn't (or we only have it from the
    # JS `window.CSRFToken = "..."` in the page), extract it from HTML and
    # stuff it into the jar so the rest of the client works consistently.
    tripletex_domain = urlparse(base_url).netloc
    csrf_token = _cookie_for_url(cookies, base_url, "CSRFTokenWriteOnly")
    if not csrf_token:
        csrf_token = extract_csrf_token(resp.text)
    if not csrf_token:
        viewer_resp = await http.get(
            f"{base_url}/execute/viewer",
            params={"contextId": context_id},
            headers={"User-Agent": _UA},
            cookies=cookies,
        )
        _collect_cookies(cookies, viewer_resp)
        csrf_token = _cookie_for_url(cookies, base_url, "CSRFTokenWriteOnly")
        if not csrf_token:
            csrf_token = extract_csrf_token(viewer_resp.text)

    if not csrf_token:
        raise RuntimeError("Could not extract CSRF token after login")

    # Ensure the jar has it (in case it only came from HTML).
    if not _cookie_for_url(cookies, base_url, "CSRFTokenWriteOnly"):
        cookies.set("CSRFTokenWriteOnly", csrf_token, domain=tripletex_domain, path="/")

    return WebSession(
        cookies=cookies,
        context_id=context_id,
    )


async def _follow_redirects(
    http: httpx.AsyncClient,
    url: str,
    cookies: httpx.Cookies,
    max_redirects: int = 15,
) -> httpx.Response:
    """Follow redirects manually, accumulating cookies."""
    for _ in range(max_redirects):
        resp = await http.get(
            url,
            headers={"User-Agent": _UA},
            cookies=cookies,
            follow_redirects=False,
        )
        _collect_cookies(cookies, resp)

        if resp.status_code in (301, 302, 303, 307, 308):
            url = _resolve_url(resp.headers["location"], url)
            continue

        return resp

    raise RuntimeError(f"Too many redirects (>{max_redirects})")


def _find_form_with_field(
    forms: list[tuple[str, str, dict[str, str]]],
    field_name: str,
) -> tuple[str, str, dict[str, str]] | None:
    """Find the first form containing a specific field."""
    for action, method, data in forms:
        if field_name in data:
            return action, method, data
    return None


def _is_login_form(data: dict[str, str]) -> bool:
    """Check if a form looks like a login form (has username/password fields)."""
    login_fields = {"Username", "Password", "AuthCode", "Totp"}
    return bool(login_fields & set(data.keys()))


def _cookie_for_url(cookies: httpx.Cookies, url: str, name: str) -> str:
    """Return the value of ``name`` that the jar would send on a request to
    ``url``. Applies proper domain/path/secure matching via ``http.cookiejar``.
    """
    req = Request(url)
    cookies.jar.add_cookie_header(req)
    header = req.get_header("Cookie", "")
    for pair in header.split("; "):
        key, _, value = pair.partition("=")
        if key == name:
            return value
    return ""


def _collect_cookies(jar: httpx.Cookies, response: httpx.Response) -> None:
    """Extract Set-Cookie headers from response and all redirect history."""
    # Collect from redirect history
    if hasattr(response, "history"):
        for hist_resp in response.history:
            for cookie in hist_resp.cookies.jar:
                jar.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
    # Collect from final response
    for cookie in response.cookies.jar:
        jar.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
