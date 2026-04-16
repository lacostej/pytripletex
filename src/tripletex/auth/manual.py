"""Manual session creation from browser cookies."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from tripletex.session import WebSession


def create_manual_session(
    cookie: str,
    context_id: str,
    csrf_token: str,
    base_url: str = "https://tripletex.no",
) -> WebSession:
    """Create a session from manually-provided browser cookies.

    Args:
        cookie: Full cookie string from browser DevTools (e.g. "JSESSIONID=abc; CSRFTokenWriteOnly=xyz")
        context_id: Tripletex context ID from URL (e.g. "32611682")
        csrf_token: CSRF token from x-tlx-csrf-token header — stored as the
            CSRFTokenWriteOnly cookie scoped to the base_url domain so that
            ``WebSession.request_headers(url)`` can look it up via the jar.
        base_url: Tripletex base URL used to scope the CSRF cookie's domain.
    """
    domain = urlparse(base_url).netloc
    cookies = httpx.Cookies()
    for part in cookie.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies.set(name.strip(), value.strip(), domain=domain, path="/")

    # Ensure CSRFTokenWriteOnly is present and matches the header token.
    cookies.set("CSRFTokenWriteOnly", csrf_token, domain=domain, path="/")

    return WebSession(
        cookies=cookies,
        context_id=context_id,
    )
