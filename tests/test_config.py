"""Tests for configuration loading."""

import os

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
