"""Core Tripletex HTTP client with session management."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from tripletex.auth.manual import create_manual_session
from tripletex.config import TripletexConfig
from tripletex.models import Company
from tripletex.session import TripletexSession


class TripletexClient:
    """Main entry point for Tripletex interactions.

    Wraps httpx.AsyncClient with session management, auto-refresh,
    and multi-company support.
    """

    def __init__(self, config: TripletexConfig) -> None:
        self.config = config
        self._session: TripletexSession | None = None
        self._http: httpx.AsyncClient | None = None

    @property
    def session(self) -> TripletexSession:
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
        """Authenticate using available credentials.

        Tries in order:
        1. Manual cookie/csrf/context-id if all three are provided
        2. Persisted session from disk
        3. Visma Connect login (interactive)
        """
        if self.config.cookie and self.config.csrf_token and self.config.context_id:
            self._session = create_manual_session(
                cookie=self.config.cookie,
                csrf_token=self.config.csrf_token,
                context_id=self.config.context_id,
            )
            return

        # Try loading persisted session
        session_path = self._session_path()
        session = TripletexSession.load(session_path)
        if session is not None:
            self._session = session
            if await self._validate_session():
                return

        # Fall back to Visma Connect login
        from tripletex.auth.visma_connect import visma_connect_login

        self._session = await visma_connect_login(self.config, self.http)
        self._session.save(session_path)

    async def _validate_session(self) -> bool:
        """Check if current session is still valid via company-chooser."""
        try:
            result = await self.get_json("/v2/internal/company-chooser")
            return result.get("status") != 401
        except (httpx.HTTPStatusError, httpx.RequestError):
            return False

    async def ensure_session(self) -> None:
        """Ensure we have a valid session, re-authenticating if needed."""
        if self._session is None:
            await self.authenticate()
            return
        if not await self._validate_session():
            await self.authenticate()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET a JSON endpoint on tripletex.no."""
        response = await self.http.get(
            path,
            params=params,
            headers=self.session.request_headers(for_json=True),
            cookies=self.session.cookies,
        )
        response.raise_for_status()
        return response.json()

    async def post_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> dict:
        """POST a JSON endpoint on tripletex.no."""
        headers = self.session.request_headers(for_json=True)
        headers["Content-Type"] = "application/json"
        headers["Origin"] = self.config.base_url
        response = await self.http.post(
            path,
            params=params,
            json=json_body,
            headers=headers,
            cookies=self.session.cookies,
        )
        response.raise_for_status()
        return response.json()

    async def get_html(self, path: str, params: dict[str, Any] | None = None) -> str:
        """GET an HTML endpoint (/execute/*)."""
        response = await self.http.get(
            path,
            params=params,
            headers=self.session.request_headers(for_json=False),
            cookies=self.session.cookies,
        )
        response.raise_for_status()
        return response.text

    async def download(
        self,
        path: str,
        params: dict[str, Any],
        dest: Path,
    ) -> Path:
        """Download binary content (PDF/image) to a file."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self.http.stream(
            "GET",
            path,
            params=params,
            headers=self.session.request_headers(for_json=False),
            cookies=self.session.cookies,
        ) as response:
            response.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)
        return dest

    async def list_companies(self) -> list[Company]:
        """List all accessible companies."""
        from tripletex.endpoints.companies import list_companies

        return await list_companies(self)

    @asynccontextmanager
    async def company_context(self, company: Company) -> AsyncIterator[TripletexClient]:
        """Context manager that temporarily switches to a different company."""
        original_context_id = self.session.context_id
        self.session.context_id = str(company.id)
        try:
            yield self
        finally:
            self.session.context_id = original_context_id

    async def iter_companies(self) -> AsyncIterator[tuple[Company, TripletexClient]]:
        """Iterate over all companies, yielding (company, client) pairs."""
        companies = await self.list_companies()
        for company in companies:
            async with self.company_context(company) as client:
                yield company, client

    async def close(self) -> None:
        """Close the HTTP client."""
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
