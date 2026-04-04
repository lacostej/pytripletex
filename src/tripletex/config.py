"""Configuration loading from env vars, TOML config file, or CLI overrides."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class TripletexConfig(BaseModel):
    """Tripletex client configuration.

    Resolution order: explicit values > env vars > config file defaults.
    """

    env_name: str | None = None  # config section name, used for session file naming
    username: str | None = None
    password_visma: str | None = None

    # Manual session overrides (skip login, use browser cookies)
    cookie: str | None = None
    csrf_token: str | None = None
    context_id: str | None = None

    base_url: str = "https://tripletex.no"
    session_dir: Path = Field(default_factory=lambda: Path.home() / ".tripletex")

    slack_webhook_url: str | None = None

    model_config = {"extra": "ignore"}


def load_config(
    config_path: str | Path | None = None,
    env_name: str | None = None,
    **overrides: str | None,
) -> TripletexConfig:
    """Load config with resolution: overrides > env vars > config file."""
    file_values: dict = {}

    # 1. Config file
    if config_path is None:
        config_path = Path.home() / ".tripletex" / "config.toml"
    else:
        config_path = Path(config_path)

    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        if env_name and env_name in data:
            file_values = data[env_name]
        elif "default" in data:
            file_values = data["default"]
        else:
            file_values = data

    # 2. Env vars
    env_map = {
        "TRIPLETEX_USERNAME": "username",
        "TRIPLETEX_PASSWORD_VISMA": "password_visma",
        "TRIPLETEX_COOKIE": "cookie",
        "TRIPLETEX_CSRF_TOKEN": "csrf_token",
        "TRIPLETEX_CONTEXT_ID": "context_id",
        "TRIPLETEX_BASE_URL": "base_url",
        "TRIPLETEX_SLACK_WEBHOOK_URL": "slack_webhook_url",
    }
    env_values = {}
    for env_key, field_name in env_map.items():
        val = os.environ.get(env_key)
        if val:
            env_values[field_name] = val

    # 3. Merge: file < env < overrides
    merged = {**file_values, **env_values}
    for k, v in overrides.items():
        if v is not None:
            merged[k] = v

    if env_name:
        merged["env_name"] = env_name

    return TripletexConfig(**merged)
