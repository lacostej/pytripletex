"""Parse JavaScript snippets embedded in Tripletex HTML pages."""

from __future__ import annotations

import re
from urllib.parse import unquote


def extract_csrf_token(html: str) -> str | None:
    """Extract CSRF token from: window.CSRFToken = "...";"""
    m = re.search(r'window\.CSRFToken\s*=\s*"([0-9a-f]+)"', html)
    return m.group(1) if m else None


def extract_js_redirect_url(html: str) -> str | None:
    """Extract redirect URL from: window.location.href=decodeURIComponent('...')"""
    m = re.search(
        r"window\.location\.href\s*=\s*decodeURIComponent\('([^']+)'\)", html
    )
    if not m:
        return None
    return unquote(m.group(1))
