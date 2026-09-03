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
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse
from urllib.request import Request

import httpx
from bs4 import BeautifulSoup

from tripletex.parsers.js import extract_csrf_token, extract_js_redirect_url
from tripletex.session import TRUST_COOKIES, InteractiveLoginRequired, WebSession

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
    #: Raw MFA page, kept so the trusted-device checkbox can be read off it at
    #: submission time rather than guessed. Not serialized — `LoginState` is
    #: in-memory only, and this holds no secret beyond what the jar already has.
    mfa_html: str = ""
    #: Whether to tick "remember this device for 30 days" when submitting.
    trust_device: bool = False


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
    """Extract all forms from HTML. Returns list of (action, method, {name: value}).

    Note what this flattening does to an ASP.NET checkbox, because it matters:
    the framework emits a checkbox and a hidden field of the *same name*, the
    hidden one last, so an unchecked box still posts a value.

        <input type="checkbox" name="RememberCode" value="true">
        <input type="hidden"   name="RememberCode" value="false">

    Collapsed into a dict, the hidden `false` overwrites the checkbox and the
    form always posts `false` — which is why every login this library has ever
    made declined to remember the device, whatever the page offered. Use
    `_checkbox_pair` and `_encode_form` to opt back in.
    """
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


def _checkbox_pair(html: str, name: str) -> tuple[str | None, bool]:
    """(checked-value, has-hidden-partner) for a named checkbox, or (None, …).

    The checked value is read from the element rather than assumed to be
    `"true"` — Visma uses `value="true"`, but `"on"` is the HTML default and
    other IdPs use it.
    """
    soup = BeautifulSoup(html, "lxml")
    checkbox = soup.find("input", attrs={"type": "checkbox", "name": name})
    if checkbox is None:
        return None, False
    hidden = soup.find("input", attrs={"type": "hidden", "name": name})
    return checkbox.get("value") or "on", hidden is not None


def _encode_form(
    data: dict[str, str], checked: dict[str, tuple[str, bool]]
) -> dict[str, str | list[str]]:
    """Form data with the named checkboxes ticked, ready to POST.

    A ticked ASP.NET checkbox is posted by a browser as *two* values — the
    checkbox's own, then the hidden partner's — and the model binder takes the
    first. We reproduce that exactly rather than sending a lone `true`, because
    matching the browser is the one shape known to work; the sniffed request
    ends `…&RememberCode=true&…&RememberCode=false`.

    The repeat is expressed as a list value rather than a list of pairs on
    purpose: httpx treats a list passed to `data=` as raw content and builds a
    *sync* byte stream from it, which blows up on an AsyncClient. A dict whose
    value is a list is the supported way to repeat a key.

    Where there is no hidden partner, a single value is correct and adding a
    second would be wrong.
    """
    encoded: dict[str, str | list[str]] = {}
    for key, value in data.items():
        if key in checked:
            on_value, has_hidden = checked[key]
            encoded[key] = [on_value, value] if has_hidden else on_value
        else:
            encoded[key] = value
    return encoded


async def start_login(
    config: TripletexConfig,
    http: httpx.AsyncClient | None = None,
    prior_cookies: httpx.Cookies | None = None,
) -> WebSession | LoginState:
    """Run the email + password steps of Visma Connect login.

    Pass `prior_cookies` — the jar from a previous, now-dead session — to let a
    trusted-device cookie carry over. That is what makes `trust_device` worth
    anything: without it the login starts from an empty jar, Visma Connect sees
    an unknown browser, and asks for a code however many times we ticked the box.

    Returns:
        WebSession — if no MFA is required (login complete, or device trusted)
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
        return await _do_login_phase1(config, http, prior_cookies)
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

        payload = _encode_form(data, _trusted_device_fields(state))

        form_url = _resolve_url(state.mfa_form_action, state.visma_base)
        resp = await http.post(
            form_url,
            data=payload,
            headers={"User-Agent": _UA},
            cookies=cookies,
            follow_redirects=True,
        )
        _collect_cookies(cookies, resp)

        return await _finish_login(resp, cookies, state.base_url, http)
    finally:
        if own_client:
            await http.aclose()


#: The MFA-step checkbox that trusts this device, in the order we prefer them.
#: `RememberCode` is what Visma Connect serves today, labelled "Remember this
#: device for 30 days"; the rest are the names neighbouring IdPs use, kept so a
#: rename does not silently turn the feature off. Nothing is guessed — a name is
#: only used when the page actually contains a checkbox with it.
_TRUST_DEVICE_FIELDS = (
    "RememberCode",
    "RememberMachine",
    "RememberBrowser",
    "TrustDevice",
)

#: Password-step checkbox that asks for a long-lived session.
_PERSISTENT_SESSION_FIELDS = (
    "RememberMe",
    "RememberLogin",
    "PersistentCookie",
    "IsPersistent",
)

#: Hidden flag by which the server says device-trust is switched off for this
#: tenant. Present and "True" means the checkbox is decorative.
_DISABLE_REMEMBER = "DisableRememberDevice"


def _tickable(
    html: str, data: dict[str, str], candidates: tuple[str, ...]
) -> dict[str, tuple[str, bool]]:
    """Which of `candidates` this page really offers, ready for `_encode_form`."""
    found: dict[str, tuple[str, bool]] = {}
    for name in candidates:
        if name not in data:
            continue
        on_value, has_hidden = _checkbox_pair(html, name)
        if on_value is not None:
            found[name] = (on_value, has_hidden)
            break  # one is enough; these are alternate spellings of one idea
    return found


def _trusted_device_fields(state: LoginState) -> dict[str, tuple[str, bool]]:
    """Checkboxes to tick on the MFA form, honouring the server's own opt-out."""
    if not state.trust_device:
        return {}

    if (state.mfa_form_data.get(_DISABLE_REMEMBER) or "").lower() == "true":
        print(
            "Visma Connect reports device-trust is disabled for this account; "
            "submitting MFA without it.",
            file=sys.stderr,
        )
        return {}

    fields = _tickable(state.mfa_html, state.mfa_form_data, _TRUST_DEVICE_FIELDS)
    if not fields:
        # Deliberately not fatal. A login that fails because a checkbox moved
        # would be worse than a login that still asks for a code.
        print(
            "No trusted-device checkbox on the MFA form; submitting without it.",
            file=sys.stderr,
        )
    else:
        print(
            f"Asking Visma Connect to remember this device ({', '.join(fields)}).",
            file=sys.stderr,
        )
    return fields


def _seed_durable_cookies(target: httpx.Cookies, source: httpx.Cookies) -> int:
    """Copy the trusted-device grant into a fresh login. Returns how many.

    **An allowlist, and it has to be.** The first attempt carried every
    persistent cookie except a handful of known-bad names, which on a real jar
    meant eight: four AWS load-balancer cookies and `isTripletexUser` from
    tripletex.no, plus `rememberUsername` and `.AspNetCore.Culture`. None of
    them have any business in a `connect.visma.com` login, and carrying them
    bounced the email step straight back to the login page.

    Only the device grant is wanted here, so only the device grant is copied.
    Everything else the server will issue fresh, which is what it does for a
    browser opening the page for the first time — and a browser is the thing we
    are imitating.

    Session-scoped copies are skipped: before the checkbox fix `remember2sv` was
    present but carried no expiry, meaning it had been issued and never granted.
    """
    now = time.time()
    copied = 0
    for cookie in source.jar:
        if not cookie.expires or cookie.expires <= now:
            continue
        if (cookie.name or "").lower() not in TRUST_COOKIES:
            continue
        target.jar.set_cookie(cookie)
        copied += 1
    return copied


async def visma_connect_login(
    config: TripletexConfig,
    http: httpx.AsyncClient | None = None,
    prior_cookies: httpx.Cookies | None = None,
) -> WebSession:
    """Perform the full Visma Connect login flow (CLI — prompts for MFA on stdin).

    With `config.trust_device` and a `prior_cookies` jar carrying an accepted
    trusted-device cookie, this completes without prompting — which is what lets
    a scheduled job repair its own session instead of waiting for a human.
    """
    if not config.username:
        raise ValueError("username required for Visma Connect login")
    if not config.password_visma:
        raise ValueError("password_visma required for Visma Connect login")

    own_client = http is None
    if http is None:
        http = httpx.AsyncClient(timeout=30.0)

    try:
        result = await _do_login_phase1(config, http, prior_cookies)

        if isinstance(result, WebSession):
            return result

        # MFA required. Only a terminal can answer it, so fail cleanly rather
        # than reading EOF from a pipe and dying later on a missing contextId.
        if not sys.stdin.isatty():
            raise InteractiveLoginRequired(config.env_name)

        print("Enter your 6-digit MFA code: ", end="", flush=True, file=sys.stderr)
        auth_code = sys.stdin.readline().strip()
        if not auth_code:
            raise InteractiveLoginRequired(config.env_name)

        return await complete_login(result, auth_code, http)
    finally:
        if own_client:
            await http.aclose()


async def _do_login_phase1(
    config: TripletexConfig,
    http: httpx.AsyncClient,
    prior_cookies: httpx.Cookies | None = None,
) -> WebSession | LoginState:
    """Email + password steps. Returns WebSession or LoginState (if MFA needed)."""
    # Step 1: Follow redirect chain from Tripletex to Visma Connect login page
    url = f"{config.base_url}/execute/login"

    # Carry a trusted-device cookie into the new login, but only when device
    # trust was actually asked for. Seeding unconditionally changed the default
    # login path for everyone and broke it — see `_seed_durable_cookies`.
    cookies = httpx.Cookies()
    if prior_cookies is not None and config.trust_device:
        carried = _seed_durable_cookies(cookies, prior_cookies)
        print(
            f"Carrying {carried} durable cookie(s) from the previous session."
            if carried
            else "Previous session held no durable cookies; logging in fresh.",
            file=sys.stderr,
        )

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
            f"{_page_complaint(resp.text)}"
            f"Forms found: {[(a, list(d.keys())) for a, _, d in forms]}"
        )

    action, _, data = password_form
    data["Password"] = config.password_visma

    checked: dict[str, tuple[str, bool]] = {}
    if config.persistent_session:
        checked = _tickable(resp.text, data, _PERSISTENT_SESSION_FIELDS)
        if checked:
            print(
                f"Asking for a long-lived session ({', '.join(checked)}).",
                file=sys.stderr,
            )
        else:
            print(
                "No stay-signed-in checkbox on the password form; continuing.",
                file=sys.stderr,
            )

    form_url = _resolve_url(action, visma_base)
    print("Submitting password...", file=sys.stderr)

    resp = await http.post(
        form_url,
        data=_encode_form(data, checked),
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
            mfa_html=resp.text,
            trust_device=config.trust_device,
        )

    # No MFA. Either this account has none, or a trusted-device cookie from a
    # previous login was accepted and Visma Connect skipped the step.
    if prior_cookies is not None:
        print("MFA not requested — device recognised.", file=sys.stderr)
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


def _page_complaint(html: str) -> str:
    """Any validation message the page is showing, as a line ready to print.

    When Visma rejects a step it re-renders the same page with the reason in a
    validation summary rather than returning an error status. Without surfacing
    it, a bounced step looks like the form moved — which sent us guessing at
    passwordless and page-layout changes twice, when the page was saying what
    was wrong all along.
    """
    soup = BeautifulSoup(html, "lxml")
    messages: list[str] = []
    for node in soup.select(
        ".validation-summary-errors, .field-validation-error, "
        ".alert-danger, .text-danger, [role=alert]"
    ):
        text = " ".join(node.get_text(" ", strip=True).split())
        if text and text not in messages:
            messages.append(text)
    if not messages:
        return "Page showed no validation message.\n"
    return "Page says: " + " | ".join(messages) + "\n"


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
    """Copy Set-Cookie from a response and its whole redirect chain into `jar`.

    **Copy the cookie object, never `jar.set(name, value, …)`.** That helper
    builds a *new* `http.cookiejar.Cookie` from the four arguments it is given
    and leaves everything else at its default — so `expires` becomes `None`,
    along with `secure` and the `rest` dict that carries HttpOnly.

    This silently flattened every persistent cookie into a session cookie on the
    way in. The session file recorded `expires: null` for all sixteen cookies,
    the trusted-device cookie included, which made a 30-day grant look like the
    server refusing to stamp a deadline at all. The data was never missing; we
    were discarding it one line after receiving it.

    Redirect history matters as much: Visma sets the interesting cookies on the
    302 from `/totp/auth`, not on the page it lands you.
    """
    responses = [*getattr(response, "history", []), response]
    for resp in responses:
        for cookie in resp.cookies.jar:
            jar.jar.set_cookie(cookie)
