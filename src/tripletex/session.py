"""Session state: web session (cookies) and API session (Basic auth)."""

from __future__ import annotations

import base64
import json
import pickle
from pathlib import Path
from typing import Protocol

import httpx


class Session(Protocol):
    """Protocol for Tripletex session auth — implemented by WebSession and ApiSession."""

    def request_headers(self, url: str, *, for_json: bool = True) -> dict[str, str]: ...
    def request_cookies(self) -> httpx.Cookies | None: ...
    def request_auth(self) -> httpx.Auth | None: ...


class AuthUnavailable(RuntimeError):
    """Base for "this run cannot authenticate the way it needs to"."""


class InteractiveLoginRequired(AuthUnavailable):
    """A web session must be refreshed, but there is no terminal to do it on.

    Raised instead of blocking on an MFA prompt that a scheduler or pipeline can
    never answer.
    """

    def __init__(self, env_name: str | None = None) -> None:
        env = f" --env {env_name}" if env_name else ""
        super().__init__(
            "The web session has expired and Visma Connect wants an MFA code, "
            "but stdin is not a terminal. Refresh it from a terminal with: "
            f"tripletex{env} login"
        )
        self.env_name = env_name


class CompanyMismatch(AuthUnavailable):
    """The credentials work, but they reach a different company than configured.

    Guards against the quiet failures: a renamed config section, a token copied
    between companies, an `--env` that fell through to another set of credentials.
    """

    def __init__(
        self, expected: int, actual: int | None, env_name: str | None = None
    ) -> None:
        env = f"'{env_name}'" if env_name else "The configured credentials"
        super().__init__(
            f"{env} authenticates to company {actual}, but the config declares "
            f"company_id {expected}. Check the section's tokens, or drop "
            f"company_id if the move was intended."
        )
        self.expected = expected
        self.actual = actual


class WebSessionRequired(AuthUnavailable):
    """An operation needs cookie/context auth that API tokens cannot provide.

    Either the endpoint rejects token auth outright (`/v2/bank/payment` and
    `/v2/voucherInbox/inboxFiltered` answer 403; the internal salary endpoints
    too), or it needs the `contextId` that only a web session carries — company
    switching and every `/execute/*` page.
    """

    def __init__(self, what: str) -> None:
        super().__init__(
            f"{what} requires a web session — re-run with --auth web "
            f"(API tokens cannot reach it)"
        )
        self.what = what


def require_web_session(session: object, what: str) -> WebSession:
    """Return `session` if it is a web session, else raise `WebSessionRequired`."""
    if not isinstance(session, WebSession):
        raise WebSessionRequired(what)
    return session


class WebSession:
    """Web session using cookies and context ID.

    The CSRF token lives in the cookie jar (``CSRFTokenWriteOnly``) and is
    pulled fresh per request by ``request_headers(url)``. This way the
    ``x-tlx-csrf-token`` header always matches what the server last set —
    no stale snapshot if the server rotates the token mid-session.
    """

    def __init__(
        self,
        cookies: httpx.Cookies,
        context_id: str,
    ) -> None:
        self.cookies = cookies
        self.context_id = context_id

    def request_headers(self, url: str, *, for_json: bool = True) -> dict[str, str]:
        # Import here to avoid a circular import (auth.visma_connect imports
        # from session for WebSession).
        from tripletex.auth.visma_connect import _cookie_for_url
        csrf = _cookie_for_url(self.cookies, url, "CSRFTokenWriteOnly")
        headers: dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/116.0",
            "Accept-Language": "en-US,en;q=0.5",
            "x-tlx-context-id": self.context_id,
            "x-tlx-csrf-token": csrf,
        }
        if for_json:
            headers["Accept"] = "application/json; charset=utf-8"
        else:
            headers["Accept"] = "*/*"
            headers["X-Requested-With"] = "XMLHttpRequest"
        return headers

    def request_cookies(self) -> httpx.Cookies | None:
        return self.cookies

    def request_auth(self) -> httpx.Auth | None:
        return None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Pickle a list of http.cookiejar.Cookie objects — they serialize
        # cleanly (domain, path, secure, expires, ...). We don't pickle the
        # CookieJar itself because it holds a non-picklable RLock.
        cookies_blob = base64.b64encode(
            pickle.dumps(list(self.cookies.jar))
        ).decode("ascii")
        data = {
            "type": "web",
            "context_id": self.context_id,
            "cookies": cookies_blob,
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path) -> WebSession | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            if data.get("type", "web") != "web":
                return None
            cookies_data = data.get("cookies")
            if not isinstance(cookies_data, str):
                return None
            cookies = httpx.Cookies()
            for cookie in pickle.loads(base64.b64decode(cookies_data)):
                cookies.jar.set_cookie(cookie)
            return cls(
                cookies=cookies,
                context_id=data["context_id"],
            )
        except (json.JSONDecodeError, KeyError, pickle.UnpicklingError, ValueError):
            return None


class ApiSession:
    """API session using HTTP Basic auth with session token."""

    def __init__(self, session_token: str, company_id: int = 0) -> None:
        self.session_token = session_token
        self.company_id = company_id

    def request_headers(self, url: str, *, for_json: bool = True) -> dict[str, str]:
        # url unused — API session uses Basic auth, no CSRF.
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if for_json:
            headers["Accept"] = "application/json; charset=utf-8"
        return headers

    def request_cookies(self) -> httpx.Cookies | None:
        return None

    def request_auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(
            username=str(self.company_id),
            password=self.session_token,
        )


# Backward compatibility alias
TripletexSession = WebSession
