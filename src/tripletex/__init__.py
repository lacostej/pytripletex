"""Tripletex Python client — web scraping + official API access."""

from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.session import ApiSession, WebSession

__all__ = ["TripletexClient", "TripletexConfig", "WebSession", "ApiSession"]
