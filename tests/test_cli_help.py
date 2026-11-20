"""Tests for `tripletex help`, the CLI's description of itself."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from tripletex.cli.main import cli


@pytest.fixture
def run(tmp_path):
    def _run(*args):
        return CliRunner().invoke(cli, ["--config", str(tmp_path / "absent.toml"), "help", *args], prog_name="tripletex")

    return _run


def _leaves(command, path=()):
    path = (*path, command["name"])
    if "commands" not in command:
        return [path]
    return [leaf for sub in command["commands"] for leaf in _leaves(sub, path)]


def test_json_describes_every_command(run):
    result = run("--json")

    assert result.exit_code == 0, result.output
    tree = json.loads(result.stdout)
    assert tree["version"] == 1
    assert tree["name"] == "tripletex"
    leaves = _leaves(tree)
    assert ("tripletex", "vouchers", "list") in leaves
    assert ("tripletex", "api") in leaves
    assert ("tripletex", "help") in leaves


def test_json_for_one_command(run):
    result = run("--json", "vouchers", "list")

    assert result.exit_code == 0, result.output
    command = json.loads(result.stdout)
    assert command["name"] == "tripletex vouchers list"
    params = {p["name"]: p for p in command["params"]}
    assert params["from_date"] == {
        "name": "from_date",
        "kind": "option",
        "type": "text",
        "opts": ["--from"],
        "required": True,
        "help": "Start date, inclusive (YYYY-MM-DD)",
    }
    assert params["as_json"]["is_flag"] is True
    assert "default" not in params["as_json"]
    assert "help" not in params


def test_json_carries_choices_defaults_and_paired_flags(run):
    root = json.loads(run("--json").stdout)
    auth = next(p for p in root["params"] if p["name"] == "auth_mode")
    assert auth["choices"] == ["web", "api", "auto"]
    assert auth["default"] == "auto"

    login = json.loads(run("--json", "login").stdout)
    trust = next(p for p in login["params"] if p["name"] == "trust_device")
    assert trust["opts"] == ["--trust-device", "--no-trust-device"]


def test_json_help_has_no_click_markup(run):
    command = json.loads(run("--json", "api").stdout)

    assert "\b" not in command["help"]
    assert command["help"].startswith("Send one request")


def test_text_mode_prints_the_usual_help(run):
    result = run("vouchers", "queue")

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("Usage: tripletex vouchers queue [OPTIONS]")


def test_unknown_command(run):
    result = run("--json", "vouchers", "nope")

    assert result.exit_code == 2
    assert "No such command: vouchers nope" in result.stderr
    assert result.stdout == ""
