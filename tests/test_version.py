"""serverInfo.version must be the package version, not FastMCP's own."""

import json
import tomllib
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_server_reports_the_package_version(_server):
    pkg = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert _server.PACKAGE_VERSION == version("hydrata-mcp-server") == pkg
    assert str(_server.mcp.version) == pkg
    assert json.loads((ROOT / "server.json").read_text())["version"] == pkg
