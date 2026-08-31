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

    # Official API auth tokens
    consumer_token: str | None = None
    employee_token: str | None = None

    # Manual session overrides (skip login, use browser cookies)
    cookie: str | None = None
    csrf_token: str | None = None
    context_id: str | None = None

    base_url: str = "https://tripletex.no"
    session_dir: Path = Field(default_factory=lambda: Path.home() / ".tripletex")

    slack_webhook_url: str | None = None

    model_config = {"extra": "ignore"}


def _section_paths(data: dict, prefix: str = "") -> list[str]:
    """Dotted paths of every table that actually holds settings."""
    paths: list[str] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        path = f"{prefix}{key}"
        if any(not isinstance(v, dict) for v in value.values()):
            paths.append(path)
        paths.extend(_section_paths(value, f"{path}."))
    return paths


def _resolve_section(data: dict, env_name: str, config_path: Path) -> dict:
    """Look up a config section by name, including dotted paths into subtables.

    `[BH.salaries]` in TOML is a subtable of `BH`, not a key literally named
    "BH.salaries", so `--env BH.salaries` has to walk the path. A miss raises
    rather than falling back to `default` — silently authenticating with another
    section's credentials is far worse than stopping.
    """
    node: object = data
    for part in env_name.split("."):
        if not isinstance(node, dict) or part not in node:
            available = ", ".join(_section_paths(data)) or "(none)"
            raise ValueError(
                f"No config section '{env_name}' in {config_path}. Available: {available}"
            )
        node = node[part]

    if not isinstance(node, dict) or all(isinstance(v, dict) for v in node.values()):
        children = ", ".join(f"{env_name}.{k}" for k in node) if isinstance(node, dict) else ""
        raise ValueError(
            f"Config section '{env_name}' holds no settings of its own"
            + (f". Did you mean: {children}?" if children else "")
        )
    return node


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
        if env_name:
            file_values = _resolve_section(data, env_name, config_path)
        elif "default" in data:
            file_values = data["default"]
        else:
            file_values = data

    # 2. Env vars
    env_map = {
        "TRIPLETEX_USERNAME": "username",
        "TRIPLETEX_PASSWORD_VISMA": "password_visma",
        "TRIPLETEX_CONSUMER_TOKEN": "consumer_token",
        "TRIPLETEX_EMPLOYEE_TOKEN": "employee_token",
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
