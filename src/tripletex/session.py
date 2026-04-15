"""Session state: web session (cookies) and API session (Basic auth)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Protocol

import httpx


class Session(Protocol):
    """Protocol for Tripletex session auth — implemented by WebSession and ApiSession."""

    def request_headers(self, *, for_json: bool = True) -> dict[str, str]: ...
    def request_cookies(self) -> httpx.Cookies | None: ...
    def request_auth(self) -> httpx.Auth | None: ...


class WebSession:
    """Web session using cookies, CSRF token, and context ID."""

    def __init__(
        self,
        cookies: httpx.Cookies,
        csrf_token: str,
        context_id: str,
    ) -> None:
        self.cookies = cookies
        self.csrf_token = csrf_token
        self.context_id = context_id

    def request_headers(self, *, for_json: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/116.0",
            "Accept-Language": "en-US,en;q=0.5",
            "x-tlx-context-id": self.context_id,
            "x-tlx-csrf-token": self.csrf_token,
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
        data = {
            "type": "web",
            "csrf_token": self.csrf_token,
            "context_id": self.context_id,
            "cookies": dict(self.cookies),
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
            cookies = httpx.Cookies()
            for name, value in data.get("cookies", {}).items():
                cookies.set(name, value)
            return cls(
                cookies=cookies,
                csrf_token=data["csrf_token"],
                context_id=data["context_id"],
            )
        except (json.JSONDecodeError, KeyError):
            return None


class ApiSession:
    """API session using HTTP Basic auth with session token."""

    def __init__(self, session_token: str, company_id: int = 0) -> None:
        self.session_token = session_token
        self.company_id = company_id

    def request_headers(self, *, for_json: bool = True) -> dict[str, str]:
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
