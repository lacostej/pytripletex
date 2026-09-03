"""Core Tripletex HTTP client with session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from tripletex.config import TripletexConfig
from tripletex.models import Company
from tripletex.rate_limit import RateLimiter, send
from tripletex.session import (
    ApiSession,
    AuthUnavailable,
    CompanyMismatch,
    Session,
    SessionExpired,
    SessionStatus,
    WebSession,
    require_web_session,
)

#: Cheap authenticated endpoint used to probe whether a session still works.
_SESSION_PROBE_PATH = "/v2/internal/company-chooser"


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
        self._limiter: RateLimiter | None = None
        # False on a sibling from for_company(), which borrows the pool.
        self._owns_http = True

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
    def limiter(self) -> RateLimiter:
        """Paces this client's requests. One per client, since the quota is
        counted per token. Replace it before the first request to share one
        limiter across clients on the same credential."""
        if self._limiter is None:
            self._limiter = RateLimiter()
        return self._limiter

    @limiter.setter
    def limiter(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

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

        await self._verify_company()

    async def _verify_company(self) -> None:
        """Check we landed in the company the config expects.

        Opt-in: sections without `company_id` are not checked. Costs one extra
        request under token auth and none under a web session, where the context
        id already names the company.
        """
        expected = self.config.company_id
        if expected is None:
            return

        session = self.session
        if isinstance(session, WebSession):
            try:
                actual: int | None = int(session.context_id)
            except (TypeError, ValueError):
                actual = None
        else:
            data = await self.get_json("/v2/token/session/>whoAmI")
            actual = (data.get("value") or {}).get("companyId")

        if actual != expected:
            raise CompanyMismatch(expected, actual, self.config.env_name)

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

        # Fall back to Visma Connect login, handing over the dead session's jar.
        # Its Tripletex cookies are spent, but a trusted-device cookie from
        # connect.visma.com outlives them by weeks, and presenting it is what
        # lets the login skip MFA. Without this the flow starts from an empty
        # jar and `trust_device` can never pay off.
        from tripletex.auth.visma_connect import visma_connect_login

        self._session = await visma_connect_login(
            self.config,
            self.http,
            prior_cookies=session.cookies if session is not None else None,
        )
        self._session.save(session_path)

    async def session_status(self) -> SessionStatus:
        """Report whether the current session is usable, and if not, why.

        Costs one lightweight request and **never blocks on stdin**, so a
        scheduler can call it to decide whether a human is needed before doing
        any real work. This is the signal that tells an operator a dashboard row
        has stopped being true.

        Returns a `SessionStatus` for any *definitive* answer. A transient
        failure — network error, or an unexpected status from the probe — is
        raised rather than reported, so "this session is dead" and "the network
        is having a bad day" are never conflated:

            try:
                status = await client.session_status()
            except (httpx.RequestError, httpx.HTTPStatusError):
                ...  # transient: retry later
            else:
                if status is not SessionStatus.VALID:
                    ...  # a human must log in
        """
        session = self._session
        if session is None:
            session = WebSession.load(self._session_path())
            if session is None:
                return SessionStatus.NEEDS_INTERACTIVE_LOGIN
            self._session = session

        try:
            await self.get_json(_SESSION_PROBE_PATH)
        except SessionExpired:
            return SessionStatus.EXPIRED
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return SessionStatus.EXPIRED
            raise
        return SessionStatus.VALID

    async def _validate_web_session(self) -> bool:
        """Whether the current web session works, collapsing every failure.

        Used by the login flow, which only needs to decide between "reuse the
        stored session" and "log in again" — a transient failure and an expired
        session both mean the latter. Callers that need to tell those apart want
        `session_status()`.
        """
        try:
            return await self.session_status() is SessionStatus.VALID
        except (httpx.HTTPStatusError, httpx.RequestError, AuthUnavailable):
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

        # Paced against the token's quota, and retried on 429/5xx. `send` does
        # not raise for status, so the 401 branch below still owns that call.
        response = await send(self.http, self.limiter, method, path, **kwargs)
        # A 401 on a web session means it died mid-run. Surface it as a typed
        # auth failure so an unattended caller can tell "get a human" from a
        # transient HTTP failure, instead of matching on a status code.
        if response.status_code == 401 and isinstance(self.session, WebSession):
            raise SessionExpired(path, self.config.env_name)
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

        # Paced like any other request — voucher backup downloads documents in a
        # loop, which is exactly the workload that exhausts the quota. Not
        # retried: the body is consumed lazily, so there is nothing to replay.
        await self.limiter.acquire()
        async with self.http.stream("GET", path, params=params, **kwargs) as response:
            self.limiter.observe(response.headers)
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

    def for_company(self, company: Company | int) -> TripletexClient:
        """A sibling client bound to `company`, sharing this one's login.

        The returned client has its own session object carrying a different
        `contextId`, but shares the cookie jar (same login), the connection pool
        and the rate limiter — the quota belongs to the credentials, not to the
        company, so siblings must not each pace themselves independently.

        Prefer this to mutating `context_id` in place. A shared session that is
        switched and restored cannot be used concurrently — two companies inside
        an `asyncio.gather` would overwrite each other's context and silently
        read the wrong company's books — and a client kept past the switch
        reverts under the caller's feet. Siblings have neither problem.

        The sibling does not own the connection pool, so closing it is a no-op;
        close the client it came from.
        """
        session = require_web_session(self.session, "Switching company")
        company_id = company if isinstance(company, int) else company.id

        sibling = TripletexClient(self.config, auth_mode=self._auth_mode)
        sibling._session = WebSession(
            cookies=session.cookies,  # same jar — the login is shared
            context_id=str(company_id),
            created_at=session.created_at,
        )
        # Properties, so the pool and limiter are created if they do not exist
        # yet and then genuinely shared rather than duplicated.
        sibling._http = self.http
        sibling._limiter = self.limiter
        sibling._owns_http = False
        return sibling

    @asynccontextmanager
    async def company_context(self, company: Company) -> AsyncIterator[TripletexClient]:
        """Yield a client bound to `company`. Kept for the `async with` shape.

        Note it yields a *different* client — using `self` inside the block
        still talks to the original company. `for_company()` is the same thing
        without the ceremony.
        """
        yield self.for_company(company)

    async def iter_companies(self) -> AsyncIterator[tuple[Company, TripletexClient]]:
        """Iterate over all companies, yielding (company, client) pairs.

        Each client stays bound to its company after the loop moves on, so the
        pairs can be collected and used later, or fanned out over concurrently.
        """
        for company in await self.list_companies():
            yield company, self.for_company(company)

    # --- Lifecycle ---

    async def close(self) -> None:
        # A sibling from for_company() borrows the pool; closing it would break
        # the client it came from, and every other sibling.
        if self._http is not None and self._owns_http:
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
