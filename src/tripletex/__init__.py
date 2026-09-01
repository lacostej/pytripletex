"""Tripletex Python client — web scraping + official API access."""

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.session import (
    ApiSession,
    AuthUnavailable,
    CompanyMismatch,
    InteractiveLoginRequired,
    SessionExpired,
    SessionStatus,
    WebSession,
    WebSessionRequired,
)

__all__ = [
    "TripletexClient",
    "TripletexConfig",
    "WebSession",
    "ApiSession",
    # The auth contract an unattended caller needs to branch on. Every failure
    # below derives from AuthUnavailable, so a CLI can catch the base and a
    # service can distinguish the cases.
    "AuthUnavailable",
    "CompanyMismatch",
    "InteractiveLoginRequired",
    "SessionExpired",
    "SessionStatus",
    "WebSessionRequired",
]
