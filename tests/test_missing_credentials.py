"""Missing credentials are an auth failure the CLI reports, not a traceback."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from tripletex.auth.visma_connect import start_login, visma_connect_login
from tripletex.cli.main import cli
from tripletex.client import TripletexClient
from tripletex.config import TripletexConfig
from tripletex.session import AuthUnavailable, MissingCredentials


async def test_api_auth_names_the_missing_tokens():
    client = TripletexClient.api(TripletexConfig(consumer_token="c", env_name="prod"))

    with pytest.raises(MissingCredentials) as exc:
        await client.authenticate()

    assert exc.value.fields == ["employee_token"]
    assert "'prod'" in str(exc.value)


@pytest.mark.parametrize("login", [start_login, visma_connect_login])
async def test_visma_login_names_the_missing_settings(login):
    with pytest.raises(MissingCredentials) as exc:
        await login(TripletexConfig(username="someone@example.com"))

    assert exc.value.fields == ["password_visma"]


def test_still_a_value_error_and_an_auth_failure():
    error = MissingCredentials(["username"], "Visma Connect login")

    assert isinstance(error, ValueError)
    assert isinstance(error, AuthUnavailable)


def test_cli_prints_the_message_not_a_traceback(tmp_path, monkeypatch):
    for var in ("TRIPLETEX_CONSUMER_TOKEN", "TRIPLETEX_EMPLOYEE_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    result = CliRunner().invoke(
        cli, ["--config", str(tmp_path / "absent.toml"), "--auth", "api", "api", "GET", "/v2/employee"]
    )

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "API auth needs consumer_token and employee_token" in result.stderr
