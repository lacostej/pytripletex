"""Session state: web session (cookies) and API session (Basic auth)."""

from __future__ import annotations

import http.cookiejar
import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import httpx

#: Version of the persisted web-session format.
#:
#: Version 1 stored cookies as a base64 pickle and was read back with
#: ``pickle.loads``. That was inert while the file stayed local and user-owned,
#: but session state is meant to travel — a browser-based refresh flow hands a
#: session to a service — and unpickling transported state is remote code
#: execution in the process holding the Tripletex credentials.
#:
#: Version 2 serialises cookie fields explicitly. Version 1 files are refused
#: rather than migrated: reading one would mean unpickling it, which is the
#: thing being removed. The cost is a single interactive login.
SESSION_FORMAT_VERSION = 2


class SessionStatus(Enum):
    """Whether a web session can be used, and if not, what is needed.

    Returned by `TripletexClient.session_status()`. A value is a *definitive*
    answer; a transient failure raises `httpx.RequestError` instead, so callers
    can tell "this session is dead, get a human" from "the network is having a
    bad day, try later" without inspecting exception strings.
    """

    VALID = "valid"
    """The session authenticates and can be used."""

    EXPIRED = "expired"
    """Session state exists but Tripletex rejects it. Needs a human to log in."""

    NEEDS_INTERACTIVE_LOGIN = "needs_interactive_login"
    """There is no session state at all to check."""


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


class SessionExpired(AuthUnavailable):
    """A web session was accepted at startup but died mid-run.

    Raised instead of a bare `httpx.HTTPStatusError` so an unattended caller can
    distinguish "the session needs a human" — which no amount of retrying fixes —
    from a transient HTTP failure, which retrying does fix.
    """

    def __init__(self, path: str, env_name: str | None = None) -> None:
        env = f" --env {env_name}" if env_name else ""
        super().__init__(
            f"The web session was rejected by {path} (401). It has expired "
            f"mid-run. Refresh it from a terminal with: tripletex{env} login"
        )
        self.path = path
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


def _cookie_to_dict(cookie: http.cookiejar.Cookie) -> dict[str, Any]:
    """Flatten a cookie to plain data — no pickle, inspectable, versionable."""
    return {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path,
        "secure": cookie.secure,
        "expires": cookie.expires,
        "version": cookie.version,
        "port": cookie.port,
        "discard": cookie.discard,
        # Carries HttpOnly, which the server sets on the session cookies.
        "rest": dict(cookie._rest or {}),
    }


def _cookie_from_dict(data: Any) -> http.cookiejar.Cookie | None:
    """Rebuild a cookie from `_cookie_to_dict` output, or None if malformed."""
    if not isinstance(data, dict):
        return None
    name, value = data.get("name"), data.get("value")
    domain, path = data.get("domain"), data.get("path")
    if not isinstance(name, str) or not isinstance(domain, str):
        return None
    if not isinstance(path, str):
        return None

    port = data.get("port")
    rest = data.get("rest")
    return http.cookiejar.Cookie(
        version=data.get("version") or 0,
        name=name,
        value=value if isinstance(value, str) else None,
        port=port if isinstance(port, str) else None,
        port_specified=isinstance(port, str),
        domain=domain,
        # A leading dot means the cookie was sent for a whole domain rather than
        # one host; both flags are derived from it rather than stored twice.
        domain_specified=domain.startswith("."),
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=bool(data.get("secure")),
        expires=data.get("expires"),
        discard=bool(data.get("discard")),
        comment=None,
        comment_url=None,
        rest=rest if isinstance(rest, dict) else {},
    )


def _parse_timestamp(raw: Any) -> datetime | None:
    """Parse an ISO timestamp, tolerating absence and malformed values."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # A naive timestamp from an older writer is assumed UTC, so `age` cannot
    # raise on a mixed-awareness subtraction.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


#: A deadline at least this far out is what asking for a persistent session or a
#: trusted device should look like. Below it, the option did not take.
PERSISTENT_THRESHOLD = timedelta(days=7)

#: Cookies that carry a long deadline and authenticate nothing. Reporting the
#: longest deadline in the jar picked `.AspNetCore.Culture` — a locale
#: preference stamped a year out — and cheerfully called the session persistent
#: on the strength of it. "Longest" is as wrong as "soonest" once the jar holds
#: preferences; what is wanted is the longest deadline among cookies that
#: actually carry authority.
_NOT_AUTHENTICATING = (
    ".aspnetcore.culture",
    "rememberusername",
    "narrowscreen",
    "istripletexuser",
    "returnurl",
    "awsalb",
    "awsalbcors",
    "awsalbtg",
    "awsalbtgcors",
)

#: Cookies related to two-step verification on the IdP.
#:
#: **`remember2sv` is not a device grant, despite the name and the 30-day
#: expiry.** Measured: a login that posted `RememberCode=false` — the box
#: explicitly unticked — was issued `remember2sv` stamped 30 days out, exactly
#: as a login that ticked it. Visma sets it on any successful 2SV, so its
#: presence says nothing about whether device trust was granted, and reporting
#: it as evidence of one was wrong.
#:
#: Kept only for the seeding diagnostic, which reports what was carried. There
#: is no known cookie that indicates a device grant, so nothing claims to read
#: one.
TWO_STEP_COOKIES = ("remember2sv", "rememberdevice", "trusteddevice")


def _is_authenticating(name: str) -> bool:
    lowered = (name or "").lower()
    return not any(lowered.startswith(prefix) for prefix in _NOT_AUTHENTICATING)


def describe_session(
    session: WebSession, last_ok_at: datetime | None = None
) -> list[str]:
    """Everything known about a stored session, as lines ready to print.

    Shared between `login` and `status` on purpose. They get read minutes apart
    while checking whether a "remember this device" option took effect, and a
    login reporting one deadline while status reports another would be worse
    than either alone. (The design, and this warning, come from ops-monitor,
    which wrote this first and can now drop its copy.)

    `last_ok_at` is a caller's record of when the session last authenticated
    successfully — pytripletex does not track it, but ops-monitor does.
    """
    now = datetime.now(timezone.utc)
    lines: list[str] = []

    age = now - session.created_at
    lines.append(
        f"established : {session.created_at:%Y-%m-%d %H:%M UTC} "
        f"({age.days}d {age.seconds // 3600}h ago)"
    )
    if last_ok_at is not None:
        since = now - last_ok_at
        lines.append(
            f"last used ok: {last_ok_at:%Y-%m-%d %H:%M UTC} "
            f"({since.days}d {since.seconds // 3600}h ago)"
        )

    # There is deliberately no "device trust" line. `remember2sv` looks like the
    # grant — right name, 30-day expiry — but a login that explicitly posted
    # RememberCode=false was issued it just the same, so it indicates nothing
    # about device trust and reporting it as a grant misled every reading.

    deadlines = [
        (name, when)
        for name, when in session.cookie_deadlines()
        if _is_authenticating(name)
    ]
    if not deadlines:
        lines.append("expires     : nothing stamped — the server decides when")
        lines.append("              this dies, which points at an idle timeout")
        lines.append("              rather than a fixed lifetime")
        return lines

    # The *longest* deadline, never the soonest. A jar holds more than the thing
    # that authenticates: `CSRFTokenWriteOnly` is rotated per request with an
    # hour-scale expiry, so reading the soonest calls a perfectly good 30-day
    # session short-lived — backwards, and it sends someone chasing a
    # non-problem.
    name, when = deadlines[-1]
    left = when - now
    remaining = (
        "already expired"
        if left.total_seconds() < 0
        else f"{left.days}d {left.seconds // 3600}h left"
    )
    lines.append(f"expires     : {when:%Y-%m-%d %H:%M UTC} ({name}) — {remaining}")

    if len(deadlines) > 1:
        soon_name, soon_when = deadlines[0]
        lines.append(
            f"              soonest {soon_when:%Y-%m-%d %H:%M UTC} ({soon_name}), "
            "likely rotated rather than fatal"
        )

    lines.append(
        "              looks persistent"
        if left >= PERSISTENT_THRESHOLD
        else "              short-lived — a persistent-session or trusted-device"
        " option, if one was offered, did not take"
    )
    return lines


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
        created_at: datetime | None = None,
    ) -> None:
        self.cookies = cookies
        self.context_id = context_id
        #: When this session was established. Observed lifetimes range from about
        #: a day to about three months and the real TTL is unknown, so recording
        #: this is the only way anyone learns the actual distribution.
        self.created_at = created_at or datetime.now(timezone.utc)

    @property
    def age(self) -> "timedelta":
        """How long ago this session was established."""
        return datetime.now(timezone.utc) - self.created_at

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

    def cookie_expiries(self) -> dict[str, datetime]:
        """Expiry per named cookie, for the ones that carry a deadline.

        This is how to tell whether asking for a trusted device or a long-lived
        session actually worked: log in with the option on, read the deadlines,
        and look for one about 30 days out. Guessing from how long the session
        survives takes 30 days to answer the same question.

        Session cookies — no expiry, gone when the browser closes — are omitted
        rather than reported as never-expiring. If the result is empty the
        server is keeping the deadline to itself, which means an idle timeout
        rather than a fixed lifetime, and a keepalive is the fix rather than a
        longer session.
        """
        out: dict[str, datetime] = {}
        for cookie in self.cookies.jar:
            if cookie.expires:
                out[cookie.name] = datetime.fromtimestamp(cookie.expires, tz=timezone.utc)
        return out

    def cookie_deadlines(self) -> list[tuple[str, datetime]]:
        """Cookies that carry an expiry, soonest first.

        Sorted so callers can take `[-1]` for the meaningful deadline and `[0]`
        to show what is merely rotating — see `describe_session`.
        """
        return sorted(self.cookie_expiries().items(), key=lambda pair: pair[1])

    def longest_lived_cookie(self) -> tuple[str, datetime] | None:
        """The cookie that outlives the rest, or None if none carry a deadline.

        **Longest, not soonest, and the difference is a trap.** A jar holds more
        than the thing that authenticates: `CSRFTokenWriteOnly` is rotated per
        request and carries its own short expiry, so judging by the earliest
        deadline reports a perfectly good 30-day login as "the option did not
        take" — backwards, and it sends you hunting a failure that never
        happened. (Found by ops-monitor while building the same readout.)

        Three shapes to read off the result:

        - a deadline weeks out — the option took;
        - a deadline hours out — it did not;
        - `None`, no cookie stamped at all — not a lifetime question. The server
          is enforcing an idle timeout, and the fix is a keepalive ping rather
          than a longer session.
        """
        expiries = self.cookie_expiries()
        if not expiries:
            return None
        name = max(expiries, key=lambda k: expiries[k])
        return name, expiries[name]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to plain JSON-safe data, independent of where it is stored.

        This is the transport format: a session established by a human in a
        browser can be handed to a service as data, without either end sharing a
        filesystem. `save()` is a thin wrapper that writes it to a file.
        """
        return {
            "version": SESSION_FORMAT_VERSION,
            "type": "web",
            "context_id": self.context_id,
            "created_at": self.created_at.isoformat(),
            "cookies": [_cookie_to_dict(c) for c in self.cookies.jar],
        }

    @classmethod
    def from_dict(cls, data: Any) -> WebSession | None:
        """Rebuild from `to_dict()` output. Returns None if the data is unusable.

        Refuses version 1 (the pickled format) rather than migrating it — reading
        one would require the `pickle.loads` this format exists to remove.
        """
        if not isinstance(data, dict):
            return None
        if data.get("type", "web") != "web":
            return None
        if data.get("version") != SESSION_FORMAT_VERSION:
            # Includes the unversioned v1 pickle format.
            return None
        context_id = data.get("context_id")
        raw_cookies = data.get("cookies")
        if not isinstance(context_id, str) or not isinstance(raw_cookies, list):
            return None

        cookies = httpx.Cookies()
        for raw in raw_cookies:
            cookie = _cookie_from_dict(raw)
            if cookie is None:
                return None
            cookies.jar.set_cookie(cookie)

        created_at = _parse_timestamp(data.get("created_at"))
        return cls(cookies=cookies, context_id=context_id, created_at=created_at)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> WebSession | None:
        if not path.exists():
            return None
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, ValueError):
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
