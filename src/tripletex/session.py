"""Session state persistence: cookies, CSRF token, context ID."""

from __future__ import annotations

import json
from pathlib import Path

import httpx


class TripletexSession:
    """Holds live Tripletex session state."""

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
        """Build common headers for Tripletex requests."""
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

    def save(self, path: Path) -> None:
        """Persist session to JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "csrf_token": self.csrf_token,
            "context_id": self.context_id,
            "cookies": dict(self.cookies),
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path) -> TripletexSession | None:
        """Load session from JSON file, or return None if not found."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
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
