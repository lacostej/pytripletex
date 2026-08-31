"""Tests for configuration loading."""

import os

import pytest

from tripletex.config import TripletexConfig, load_config


class TestConfig:
    def test_defaults(self):
        config = TripletexConfig()
        assert config.base_url == "https://tripletex.no"
        assert config.username is None

    def test_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[bonita]
username = "test@example.com"
password_visma = "visma_secret"
""")
        config = load_config(config_path=config_file, env_name="bonita")
        assert config.username == "test@example.com"
        assert config.password_visma == "visma_secret"
        assert config.env_name == "bonita"

    def test_overrides_win(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[bonita]
username = "file@example.com"
""")
        config = load_config(
            config_path=config_file,
            env_name="bonita",
            username="override@example.com",
        )
        assert config.username == "override@example.com"

    def test_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRIPLETEX_USERNAME", "env@example.com")
        config = load_config(config_path=tmp_path / "nonexistent.toml")
        assert config.username == "env@example.com"

    def test_missing_file_ok(self, tmp_path):
        config = load_config(config_path=tmp_path / "missing.toml")
        assert config.username is None


class TestSectionResolution:
    """`--env` picking the wrong credentials silently is worse than failing."""

    def _write(self, tmp_path, body):
        p = tmp_path / "config.toml"
        p.write_text(body)
        return p

    def test_unknown_env_raises_instead_of_falling_back_to_default(self, tmp_path):
        path = self._write(tmp_path, """
[default]
username = "web-user"

[prod]
consumer_token = "c"
employee_token = "e"
""")
        with pytest.raises(ValueError, match="No config section 'typo'"):
            load_config(config_path=path, env_name="typo")

    def test_dotted_env_name_walks_into_a_subtable(self, tmp_path):
        # [BH.salaries] is a subtable of BH, not a key named "BH.salaries".
        path = self._write(tmp_path, """
[BH.salaries]
consumer_token = "c-bh"
employee_token = "e-bh"

[BS.salaries]
consumer_token = "c-bs"
employee_token = "e-bs"
""")
        assert load_config(config_path=path, env_name="BH.salaries").employee_token == "e-bh"
        assert load_config(config_path=path, env_name="BS.salaries").employee_token == "e-bs"

    def test_group_without_settings_suggests_its_children(self, tmp_path):
        path = self._write(tmp_path, """
[BH.salaries]
consumer_token = "c"
""")
        with pytest.raises(ValueError, match="BH.salaries"):
            load_config(config_path=path, env_name="BH")
