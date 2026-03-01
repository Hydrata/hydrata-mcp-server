"""Tests for hydrata_mcp.config.Config."""

import pytest

from hydrata_mcp.config import Config


class TestFromEnv:
    def test_missing_api_url_raises(self, monkeypatch):
        monkeypatch.setenv("HYDRATA_API_USERNAME", "u")
        monkeypatch.setenv("HYDRATA_API_PASSWORD", "p")
        with pytest.raises(RuntimeError, match="HYDRATA_API_URL"):
            Config.from_env()

    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.setenv("HYDRATA_API_URL", "https://example.com")
        with pytest.raises(RuntimeError, match="HYDRATA_API_USERNAME"):
            Config.from_env()

    def test_valid_env_produces_config(self, env_vars):
        cfg = Config.from_env()
        assert cfg.api_url == "https://hydrata.example.com/api/v2/anuga"
        assert cfg.api_username == "testuser"
        assert cfg.api_password == "testpass"
        assert cfg.port == 8001
        assert cfg.host == "127.0.0.1"

    def test_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("HYDRATA_API_URL", "https://example.com/api/")
        monkeypatch.setenv("HYDRATA_API_USERNAME", "u")
        monkeypatch.setenv("HYDRATA_API_PASSWORD", "p")
        cfg = Config.from_env()
        assert cfg.api_url == "https://example.com/api"

    def test_custom_port(self, monkeypatch, env_vars):
        monkeypatch.setenv("HYDRATA_MCP_PORT", "9999")
        cfg = Config.from_env()
        assert cfg.port == 9999

    def test_custom_host(self, monkeypatch, env_vars):
        monkeypatch.setenv("HYDRATA_MCP_HOST", "0.0.0.0")
        cfg = Config.from_env()
        assert cfg.host == "0.0.0.0"
