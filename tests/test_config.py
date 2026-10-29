"""Tests for hydrata_mcp.config.Config."""

from dataclasses import fields

import pytest

from hydrata_mcp.config import Config


class TestFromEnv:
    def test_missing_api_url_raises(self):
        with pytest.raises(RuntimeError, match="HYDRATA_API_URL"):
            Config.from_env()

    def test_no_credentials_ok(self, monkeypatch):
        # TASK-3168 (W0.3, epic 2467) — D2: the server holds no identity. The
        # caller's own credential (an hk_ agent key since TASK-3508) is forwarded per request (W0.1), so the
        # URL is the only required variable and the retired server-held
        # credential fields are gone from Config, not merely optional (the exact
        # field set is pinned so they cannot creep back as dead config).
        monkeypatch.setenv("HYDRATA_API_URL", "https://example.com")
        cfg = Config.from_env()
        assert cfg.api_url == "https://example.com"
        # TASK-3730: no Basic grace-period setting either: Basic on /mcp/ is
        # refused outright, so there is nothing to configure.
        # TASK-3512: resource_url is a public URL (the connect flow switch), not a credential.
        # TASK-3511: data_envelope is the harness's negative-control switch, not a credential.
        assert {f.name for f in fields(cfg)} == {
            "api_url",
            "port",
            "host",
            "api_host",
            "ui_origin",
            "data_envelope",
            "resource_url",
        }

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, True),
            ("on", True),
            ("1", True),
            ("off", False),
            ("OFF", False),
            ("0", False),
            ("false", False),
            ("no", False),
        ],
    )
    def test_data_envelope_is_on_unless_switched_off(self, monkeypatch, raw, expected):
        # TASK-3511: ON by default; only the harness's negative control turns it off.
        monkeypatch.setenv("HYDRATA_API_URL", "https://example.com")
        if raw is not None:
            monkeypatch.setenv("HYDRATA_MCP_DATA_ENVELOPE", raw)
        assert Config.from_env().data_envelope is expected

    def test_valid_env_produces_config(self, env_vars):
        cfg = Config.from_env()
        assert cfg.api_url == "https://hydrata.example.com/api/v2/anuga"
        assert cfg.port == 8001
        assert cfg.host == "127.0.0.1"

    def test_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("HYDRATA_API_URL", "https://example.com/api/")
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
