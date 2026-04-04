"""Automated Visma Connect login flow.

The current flow (2026):
1. GET tripletex.no/execute/login → redirect chain to connect.visma.com
2. Submit email (POST / with Username field)
3. Submit password (POST /login/password with Password field)
4. Submit MFA code (POST /login/totp or similar)
5. Follow redirects back to tripletex.no
6. Extract contextId from final URL
"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from tripletex.parsers.js import extract_csrf_token, extract_js_redirect_url
from tripletex.session import TripletexSession

if TYPE_CHECKING:
    from tripletex.config import TripletexConfig

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/116.0"


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


async def visma_connect_login(
    config: TripletexConfig,
    http: httpx.AsyncClient | None = None,
) -> TripletexSession:
    """Perform the full Visma Connect login flow."""
    if not config.username:
        raise ValueError("username required for Visma Connect login")
    if not config.password_visma:
        raise ValueError("password_visma required for Visma Connect login")

    own_client = http is None
    if http is None:
        http = httpx.AsyncClient(timeout=30.0)

    try:
        return await _do_login(config, http)
    finally:
        if own_client:
            await http.aclose()


async def _do_login(
    config: TripletexConfig,
    http: httpx.AsyncClient,
) -> TripletexSession:
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
        # Maybe we got redirected — check for other patterns
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
        print("Enter your 6-digit MFA code: ", end="", flush=True, file=sys.stderr)
        auth_code = sys.stdin.readline().strip()

        if "AuthCode" in data:
            data["AuthCode"] = auth_code
        elif "Totp" in data:
            data["Totp"] = auth_code

        form_url = _resolve_url(action, visma_base)
        resp = await http.post(
            form_url,
            data=data,
            headers={"User-Agent": _UA},
            cookies=cookies,
            follow_redirects=True,
        )
        _collect_cookies(cookies, resp)

    # Step 5: We should now be back on tripletex.no (or need to follow more redirects)
    # Check for JS-based redirects or further HTML redirects
    final_url = str(resp.url)
    max_redirects = 10

    for _ in range(max_redirects):
        if "tripletex.no" in final_url and "contextId" in final_url:
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
        raise RuntimeError(
            f"Could not extract contextId. Final URL: {final_url}\n"
            f"Response snippet: {resp.text[:500]}"
        )

    context_id = context_match.group(1)

    # Step 7: Get CSRF token from the dashboard/viewer page
    csrf_token = extract_csrf_token(resp.text)
    if not csrf_token:
        # Try loading the viewer page to get CSRF
        viewer_resp = await http.get(
            f"{config.base_url}/execute/viewer",
            params={"contextId": context_id},
            headers={"User-Agent": _UA},
            cookies=cookies,
        )
        _collect_cookies(cookies, viewer_resp)
        csrf_token = extract_csrf_token(viewer_resp.text)

    if not csrf_token:
        # Try the CSRFTokenWriteOnly cookie as fallback
        csrf_token = cookies.get("CSRFTokenWriteOnly", "")

    if not csrf_token:
        raise RuntimeError("Could not extract CSRF token after login")

    return TripletexSession(
        cookies=cookies,
        csrf_token=csrf_token,
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
