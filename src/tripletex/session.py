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
