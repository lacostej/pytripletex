"""Manual session creation from browser cookies."""

from __future__ import annotations

import httpx

from tripletex.session import TripletexSession


def create_manual_session(
    cookie: str,
    context_id: str,
    csrf_token: str,
) -> TripletexSession:
    """Create a session from manually-provided browser cookies.

    Args:
        cookie: Full cookie string from browser DevTools (e.g. "JSESSIONID=abc; CSRFTokenWriteOnly=xyz")
        context_id: Tripletex context ID from URL (e.g. "32611682")
        csrf_token: CSRF token from x-tlx-csrf-token header
    """
    cookies = httpx.Cookies()
    for part in cookie.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies.set(name.strip(), value.strip())

    return TripletexSession(
        cookies=cookies,
        csrf_token=csrf_token,
        context_id=context_id,
    )
