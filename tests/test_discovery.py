"""TASK-4177: anonymous catalog discovery for allowlisted MCP catalog crawlers, and the static Server Card.

RequireAuth 401s every anonymous request (TASK-3987: a 200 on an anonymous
initialize made claude.ai's Add custom connector dialog default to "No sign-in").
The one exception: a request with NO Authorization header whose User-Agent
contains an allowlisted crawler substring may run initialize,
notifications/initialized, ping and tools/list. tools/call never.
"""

import json
from pathlib import Path

import pytest
import respx

from hydrata_mcp.config import DEFAULT_DISCOVERY_USER_AGENTS, Config
from tests.helpers import (
    CALLER_OAUTH,
    INITIALIZE_PARAMS,
    MCP_HEADERS,
    _asgi_client,
    _parse,
    _rpc,
    _tools_call,
    reload_server,
)

pytestmark = pytest.mark.asyncio

TESTS = Path(__file__).resolve().parent
ROOT_SERVER_JSON = json.loads((TESTS.parent / "server.json").read_text())
SNAPSHOT_NAMES = sorted(tool["name"] for tool in json.loads((TESTS / "snapshots" / "tools.json").read_text()))
CHALLENGE = 'Bearer realm="hydrata.com"'
RESOURCE = "https://hydrata.example.com/mcp/"
MCPTRY = "mcptry.com indexer"
SMITHERY = "Mozilla/5.0 (compatible; SmitheryBot/1.0; +https://smithery.ai)"
# What real clients send: none of them may ever match the allowlist.
DEFAULT_AGENTS = [
    pytest.param("python-httpx/0.28.1", id="httpx"),
    pytest.param("Claude-User", id="claude-user"),
    pytest.param("claude-code/2.1.259", id="claude-code"),
    pytest.param("openai-mcp/1.0.0", id="chatgpt"),
    pytest.param("Cursor/1.7", id="cursor"),
    pytest.param("node", id="vscode-node"),
    pytest.param("curl/8", id="curl"),
    pytest.param("", id="empty"),
]
DISCOVERY = [
    pytest.param("initialize", INITIALIZE_PARAMS, id="initialize"),
    pytest.param("tools/list", {}, id="tools-list"),
    pytest.param("ping", {}, id="ping"),
]


def _headers(user_agent, **extra):
    return {**MCP_HEADERS, "User-Agent": user_agent, **extra}


async def _post(srv, user_agent, body, **extra):
    async with _asgi_client(srv) as c:
        return await c.post("/", headers=_headers(user_agent, **extra), json=body)


def _assert_refused(resp, challenge=CHALLENGE):
    assert resp.status_code == 401, resp.text
    assert resp.headers["www-authenticate"] == challenge
    assert resp.json()["error"]["code"] == -32001


class TestCrawlerDiscovery:
    @respx.mock  # assert_all_mocked: any request to the Hydrata API would raise
    @pytest.mark.parametrize("user_agent", [MCPTRY, SMITHERY])
    @pytest.mark.parametrize("method,params", DISCOVERY)
    async def test_crawler_discovery_method_is_200(self, _server, user_agent, method, params):
        resp = await _post(_server, user_agent, _rpc(method, params, id=3))
        assert resp.status_code == 200, resp.text
        payload = _parse(resp)
        assert "error" not in payload and payload["id"] == 3
        assert not respx.calls  # listing the catalog never reaches the Hydrata API

    @respx.mock
    async def test_crawler_tools_list_is_the_full_catalog(self, _server):
        resp = await _post(_server, MCPTRY, _rpc("tools/list"))
        tools = _parse(resp)["result"]["tools"]
        assert len(tools) == 44
        assert sorted(t["name"] for t in tools) == SNAPSHOT_NAMES

    async def test_crawler_initialized_notification_is_accepted(self, _server):
        resp = await _post(_server, MCPTRY, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert resp.status_code == 202, resp.text

    @respx.mock
    async def test_crawler_tools_call_is_401_with_todays_challenge(self, _server):
        resp = await _post(_server, MCPTRY, _tools_call(id=9))
        _assert_refused(resp)
        assert resp.json()["id"] == 9
        assert "Authorization: Bearer hk_" in resp.json()["error"]["message"]
        assert not respx.calls

    async def test_crawler_tools_call_challenge_names_the_resource_metadata(self, env_vars, monkeypatch):
        """With the connect flow on (prod), the crawler's refusal is the same OAuth challenge real clients get."""
        monkeypatch.setenv("HYDRATA_MCP_RESOURCE_URL", RESOURCE)
        srv = reload_server()
        resp = await _post(srv, MCPTRY, _tools_call())
        _assert_refused(
            resp,
            CHALLENGE + ', resource_metadata="https://hydrata.example.com/.well-known/oauth-protected-resource/mcp/"',
        )

    @pytest.mark.parametrize("method", ["resources/list", "prompts/list", "completion/complete", "logging/setLevel"])
    async def test_crawler_other_methods_are_401(self, _server, method):
        _assert_refused(await _post(_server, MCPTRY, _rpc(method)))

    async def test_crawler_response_message_without_method_is_401(self, _server):
        _assert_refused(await _post(_server, MCPTRY, {"jsonrpc": "2.0", "id": 1, "result": {}}))

    async def test_crawler_batch_with_a_tools_call_is_401(self, _server):
        resp = await _post(_server, MCPTRY, [_rpc("tools/list", id=1), _tools_call(id=2)])
        _assert_refused(resp)
        assert resp.json()["id"] == 2

    @pytest.mark.parametrize(
        "authorization",
        [
            pytest.param("Bearer not-a-key", id="bad-bearer"),
            pytest.param(CALLER_OAUTH, id="geonode-oauth"),
            pytest.param("Basic Zm9vOmJhcg==", id="basic"),
            pytest.param(" ", id="blank"),
        ],
    )
    @pytest.mark.parametrize("method,params", DISCOVERY)
    async def test_crawler_with_any_authorization_header_follows_todays_rules(
        self, _server, authorization, method, params
    ):
        """The allowlist applies only with NO Authorization header: a bad credential still 401s."""
        _assert_refused(await _post(_server, MCPTRY, _rpc(method, params), Authorization=authorization))

    @pytest.mark.parametrize("user_agent", DEFAULT_AGENTS)
    @pytest.mark.parametrize("method,params", DISCOVERY)
    async def test_default_user_agent_is_401_exactly_as_today(self, _server, user_agent, method, params):
        resp = await _post(_server, user_agent, _rpc(method, params, id=4))
        _assert_refused(resp)
        assert resp.json()["id"] == 4

    @pytest.mark.parametrize("user_agent", ["MCPTRY.COM INDEXER", "smitherybot/2.0", "x mcptry.com Indexer/1 y"])
    async def test_crawler_match_is_case_insensitive_substring(self, _server, user_agent):
        assert (await _post(_server, user_agent, _rpc("initialize", INITIALIZE_PARAMS))).status_code == 200

    async def test_crawler_allowlist_env_override(self, env_vars, monkeypatch):
        monkeypatch.setenv("HYDRATA_MCP_DISCOVERY_USER_AGENTS", " ExampleCatalogBot , other-indexer ")
        srv = reload_server()
        assert srv.config.discovery_user_agents == ("examplecatalogbot", "other-indexer")
        assert (await _post(srv, "examplecatalogbot/3", _rpc("tools/list"))).status_code == 200
        # The override REPLACES the default list.
        _assert_refused(await _post(srv, MCPTRY, _rpc("tools/list")))
        _assert_refused(await _post(srv, "examplecatalogbot/3", _tools_call()))

    @pytest.mark.parametrize("raw", ["", " ", ",", " , "])
    async def test_crawler_allowlist_empty_disables(self, env_vars, monkeypatch, raw):
        monkeypatch.setenv("HYDRATA_MCP_DISCOVERY_USER_AGENTS", raw)
        srv = reload_server()
        assert srv.config.discovery_user_agents == ()
        for user_agent in (MCPTRY, SMITHERY, ""):
            _assert_refused(await _post(srv, user_agent, _rpc("initialize", INITIALIZE_PARAMS)))

    async def test_crawler_allowlist_default(self, env_vars):
        assert DEFAULT_DISCOVERY_USER_AGENTS == ("mcptry.com indexer", "SmitheryBot")
        assert Config.from_env().discovery_user_agents == ("mcptry.com indexer", "smitherybot")

    async def test_crawler_discovery_is_not_recorded_by_telemetry(self, _server, capsys):
        """ToolCallRecorder writes only per tools/call: anonymous discovery adds no mcp_tool_call line."""
        capsys.readouterr()
        for method, params in (("initialize", INITIALIZE_PARAMS), ("tools/list", {}), ("ping", {})):
            assert (await _post(_server, MCPTRY, _rpc(method, params))).status_code == 200
        assert "mcp_tool_call" not in capsys.readouterr().out


CARD_FIELDS = ("name", "title", "description", "version", "websiteUrl", "repository", "icons", "remotes")


class TestServerCard:
    async def test_server_card_get_is_200_built_from_server_json(self, _server):
        async with _asgi_client(_server) as c:
            resp = await c.get("/server-card")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        card = resp.json()
        assert card["$schema"] == "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
        for key in CARD_FIELDS:
            assert card[key] == ROOT_SERVER_JSON[key], key
        # A card deliberately carries no tools (SEP-2127): they are read live with tools/list.
        assert set(card) == {"$schema", *CARD_FIELDS}
        assert card["version"] == _server.PACKAGE_VERSION

    async def test_server_card_needs_no_credential_and_has_cors_and_caching(self, _server):
        async with _asgi_client(_server) as c:
            resp = await c.get("/server-card", headers={"Accept": "application/mcp-server-card+json"})
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("application/mcp-server-card+json")
            assert resp.headers["access-control-allow-origin"] == "*"
            assert resp.headers["cache-control"] == "public, max-age=3600"
            etag = resp.headers["etag"]
            again = await c.get("/server-card", headers={"If-None-Match": etag})
            assert again.status_code == 304 and again.content == b""
            preflight = await c.options("/server-card")
            assert preflight.status_code == 204
            assert preflight.headers["access-control-allow-methods"] == "GET"

    async def test_server_card_is_not_the_mcp_endpoint(self, _server):
        """The card route does not open the MCP endpoint: an anonymous POST there is not a JSON-RPC 200."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/server-card", headers=MCP_HEADERS, json=_rpc("tools/list"))
        assert resp.status_code in (401, 405)


ICON_URL = "https://hydrata.com/mcp/icon.png"
ICON_FILE = TESTS.parent / "hydrata_mcp" / "assets" / "icon.png"


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk (no image library needed)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


class TestIcon:
    """The catalog icon: a square PNG the server serves itself, so its URL is on the server's own origin."""

    def test_server_json_lists_the_square_icon_on_the_server_origin(self):
        assert ROOT_SERVER_JSON["icons"] == [{"src": ICON_URL, "mimeType": "image/png", "sizes": ["512x512"]}]

    def test_icon_file_is_a_512_square_png(self):
        assert _png_size(ICON_FILE.read_bytes()) == (512, 512)

    async def test_icon_get_is_200_png_without_a_credential(self, _server):
        async with _asgi_client(_server) as c:
            resp = await c.get("/icon.png")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "image/png"
            assert resp.headers["access-control-allow-origin"] == "*"
            assert resp.headers["cache-control"] == "public, max-age=86400"
            assert resp.content == ICON_FILE.read_bytes()
            again = await c.get("/icon.png", headers={"If-None-Match": resp.headers["etag"]})
            assert again.status_code == 304

    async def test_initialize_server_info_carries_the_same_icon(self, _server):
        resp = await _post(_server, MCPTRY, _rpc("initialize", INITIALIZE_PARAMS))
        icons = _parse(resp)["result"]["serverInfo"]["icons"]
        assert icons == ROOT_SERVER_JSON["icons"]
