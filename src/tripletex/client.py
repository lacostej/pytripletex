"""Core Tripletex HTTP client with session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from tripletex.config import TripletexConfig
from tripletex.models import Company
from tripletex.session import ApiSession, Session, WebSession


class TripletexClient:
    """Main entry point for Tripletex interactions.

    Use factory methods to create clients with explicit auth:
        TripletexClient.web(config)  — web session (Visma Connect)
        TripletexClient.api(config)  — official API (token-based)
        TripletexClient(config)      — auto-detect based on config
    """

    def __init__(self, config: TripletexConfig, *, auth_mode: str | None = None) -> None:
        self.config = config
        self._auth_mode = auth_mode  # "web", "api", or None (auto-detect)
        self._session: Session | None = None
        self._http: httpx.AsyncClient | None = None

    @classmethod
    def web(cls, config: TripletexConfig) -> TripletexClient:
        """Create a client that uses web session auth (Visma Connect)."""
        return cls(config, auth_mode="web")

    @classmethod
    def api(cls, config: TripletexConfig) -> TripletexClient:
        """Create a client that uses official API token auth."""
        return cls(config, auth_mode="api")

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return self._session

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.config.base_url,
                follow_redirects=True,
                timeout=30.0,
            )
        return self._http

    async def authenticate(self) -> None:
        """Authenticate using the configured auth mode."""
        mode = self._auth_mode or self._detect_auth_mode()

        if mode == "api":
            await self._authenticate_api()
        else:
            await self._authenticate_web()

    def _detect_auth_mode(self) -> str:
        """Auto-detect: use API if tokens are configured, otherwise web."""
        if self.config.consumer_token and self.config.employee_token:
            return "api"
        return "web"

    async def _authenticate_api(self) -> None:
        """Authenticate via official API tokens."""
        from tripletex.auth.api_token import create_api_session

        if not self.config.consumer_token or not self.config.employee_token:
            raise ValueError("consumer_token and employee_token required for API auth")

        self._session = await create_api_session(
            base_url=self.config.base_url,
            consumer_token=self.config.consumer_token,
            employee_token=self.config.employee_token,
        )

    async def _authenticate_web(self) -> None:
        """Authenticate via web session (manual cookies or Visma Connect)."""
        if self.config.cookie and self.config.csrf_token and self.config.context_id:
            from tripletex.auth.manual import create_manual_session

            self._session = create_manual_session(
                cookie=self.config.cookie,
                csrf_token=self.config.csrf_token,
                context_id=self.config.context_id,
                base_url=self.config.base_url,
            )
            return

        # Try loading persisted session
        session_path = self._session_path()
        session = WebSession.load(session_path)
        if session is not None:
            self._session = session
            if await self._validate_web_session():
                return

        # Fall back to Visma Connect login
        from tripletex.auth.visma_connect import visma_connect_login

        self._session = await visma_connect_login(self.config, self.http)
        self._session.save(session_path)

    async def _validate_web_session(self) -> bool:
        """Check if current web session is still valid."""
        try:
            result = await self.get_json("/v2/internal/company-chooser")
            return result.get("status") != 401
        except (httpx.HTTPStatusError, httpx.RequestError):
            return False

    async def ensure_session(self) -> None:
        """Ensure we have a valid session, re-authenticating if needed."""
        if self._session is None:
            await self.authenticate()

    # --- HTTP methods ---

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        for_json: bool = True,
    ) -> httpx.Response:
        """Make an authenticated request."""
        url = httpx.URL(path) if path.startswith("http") else self.http.base_url.join(path)
        headers = self.session.request_headers(str(url), for_json=for_json)
        if method in ("POST", "PUT") and for_json:
            headers["Content-Type"] = "application/json"
            if isinstance(self.session, WebSession):
                headers["Origin"] = self.config.base_url

        kwargs: dict[str, Any] = {
            "headers": headers,
        }
        if self.session.request_cookies() is not None:
            kwargs["cookies"] = self.session.request_cookies()
        if self.session.request_auth() is not None:
            kwargs["auth"] = self.session.request_auth()
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body

        response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET a JSON endpoint."""
        response = await self._request("GET", path, params=params)
        return response.json()

    async def post_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> dict:
        """POST a JSON endpoint."""
        response = await self._request("POST", path, params=params, json_body=json_body)
        return response.json()

    async def put_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> dict:
        """PUT a JSON endpoint."""
        response = await self._request("PUT", path, params=params, json_body=json_body)
        return response.json()

    async def delete_json(self, path: str, params: dict[str, Any] | None = None) -> dict | None:
        """DELETE a JSON endpoint."""
        response = await self._request("DELETE", path, params=params)
        if response.content:
            return response.json()
        return None

    async def get_html(self, path: str, params: dict[str, Any] | None = None) -> str:
        """GET an HTML endpoint (/execute/*)."""
        response = await self._request("GET", path, params=params, for_json=False)
        return response.text

    async def download(
        self,
        path: str,
        params: dict[str, Any],
        dest: Path,
    ) -> Path:
        """Download binary content (PDF/image) to a file."""
        dest.parent.mkdir(parents=True, exist_ok=True)

        url = httpx.URL(path) if path.startswith("http") else self.http.base_url.join(path)
        headers = self.session.request_headers(str(url), for_json=False)
        kwargs: dict[str, Any] = {"headers": headers}
        if self.session.request_cookies() is not None:
            kwargs["cookies"] = self.session.request_cookies()
        if self.session.request_auth() is not None:
            kwargs["auth"] = self.session.request_auth()

        async with self.http.stream("GET", path, params=params, **kwargs) as response:
            response.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)
        return dest

    # --- Multi-company (web session only) ---

    async def list_companies(self) -> list[Company]:
        """List all accessible companies (web session only)."""
        from tripletex.endpoints.companies import list_companies

        return await list_companies(self)

    @asynccontextmanager
    async def company_context(self, company: Company) -> AsyncIterator[TripletexClient]:
        """Context manager that temporarily switches to a different company."""
        session = self.session
        if not isinstance(session, WebSession):
            raise RuntimeError("company_context requires web session auth")
        original_context_id = session.context_id
        session.context_id = str(company.id)
        try:
            yield self
        finally:
            session.context_id = original_context_id

    async def iter_companies(self) -> AsyncIterator[tuple[Company, TripletexClient]]:
        """Iterate over all companies, yielding (company, client) pairs."""
        companies = await self.list_companies()
        for company in companies:
            async with self.company_context(company) as client:
                yield company, client

    # --- Lifecycle ---

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> TripletexClient:
        await self.authenticate()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _session_path(self) -> Path:
        name = self.config.env_name or "default"
        return self.config.session_dir / f"session_{name}.json"
