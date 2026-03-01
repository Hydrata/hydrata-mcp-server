"""Shared fixtures for Hydrata MCP server tests."""

import pytest

from hydrata_mcp.config import Config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove Hydrata env vars so tests start clean."""
    for key in (
        "HYDRATA_API_URL",
        "HYDRATA_API_USERNAME",
        "HYDRATA_API_PASSWORD",
        "HYDRATA_MCP_PORT",
        "HYDRATA_MCP_HOST",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def env_vars(monkeypatch):
    """Set valid Hydrata env vars and return them as a dict."""
    vals = {
        "HYDRATA_API_URL": "https://hydrata.example.com/api/v2/anuga",
        "HYDRATA_API_USERNAME": "testuser",
        "HYDRATA_API_PASSWORD": "testpass",
    }
    for k, v in vals.items():
        monkeypatch.setenv(k, v)
    return vals


@pytest.fixture
def config(env_vars):
    """Build a Config from the test env vars."""
    return Config.from_env()
