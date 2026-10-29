"""The ASGI app: credential pass-through, CLI entry, client headers, annotations, structured content, agent keys."""

import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    BOUNDARY_ALTERNATE,
    CALLER_BASIC,
    CALLER_BASIC_HOLDING_KEY,
    CALLER_KEY,
    CALLER_OAUTH,
    EVERY_METHOD,
    EXEC_ID,
    EXEC_STATUS_URL,
    INITIALIZE_PARAMS,
    KEY_HEADERS,
    MCP_HEADERS,
    ORIGIN,
    UPSTREAM_OK,
    _asgi_client,
    _call,
    _call_as_caller,
    _dataset,
    _exec,
    _mock_layer_summary,
    _mock_project,
    _parse,
    _row,
    _rpc,
    _schema_args,
    _tool_json,
    _tools_call,
    reload_server,
)

pytestmark = pytest.mark.asyncio


class TestPassthrough:
    """The MCP server holds no identity of its own: it forwards the caller's."""

    @respx.mock
    async def test_anon_tools_call_401_with_agent_key_challenge(self, _server):
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_tools_call(id=7))
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Bearer realm="hydrata.com"'
        body = resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 7
        assert body["error"]["code"] == -32001
        # TASK-3508: points to agent keys, never asks for a password.
        assert "Authorization: Bearer hk_" in body["error"]["message"]
        assert "/agents/" in body["error"]["message"]
        assert "password" not in body["error"]["message"].lower()
        # Short-circuited in the middleware: nothing reached the upstream API.
        assert not upstream.calls

    @pytest.mark.parametrize("method,params", EVERY_METHOD)
    async def test_anon_every_method_is_401_with_challenge(self, _server, method, params):
        """TASK-3987: the server requires sign-in for EVERY method, not only tools/call.

        claude.ai's Add custom connector dialog read the old 200 on an anonymous
        initialize as an authless server and pre-selected "No sign-in". A 401 +
        ``WWW-Authenticate`` on initialize / tools/list is what an auth-required
        server answers (MCP authorization spec; claude.com connector auth docs).
        Supersedes decision D2 on TASK-2467 (anonymous catalog)."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_rpc(method, params, id=5))
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Bearer realm="hydrata.com"'
        body = resp.json()
        assert body["id"] == 5
        assert body["error"]["code"] == -32001
        assert "Authorization: Bearer hk_" in body["error"]["message"]

    async def test_anon_notification_is_401(self, _server):
        """A JSON-RPC notification (no id) is refused too; the error carries id null."""
        async with _asgi_client(_server) as c:
            resp = await c.post(
                "/", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        assert resp.status_code == 401
        assert resp.json()["id"] is None

    @pytest.mark.parametrize("method,params", EVERY_METHOD)
    async def test_agent_key_every_method_is_200(self, _server, method, params):
        """An hk_ agent key opens every method; initialize/tools/list need no upstream call."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc(method, params))
        assert resp.status_code == 200
        assert "error" not in _parse(resp)

    async def test_anon_batch_containing_tools_call_401(self, _server):
        batch = [_rpc("tools/list", id=1), _tools_call(id=2)]
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=batch)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Bearer realm="hydrata.com"'
        assert resp.json()["id"] == 2

    @respx.mock
    async def test_agent_key_header_forwarded_verbatim(self, _server):
        upstream = respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(200, json={"count": 3, "results": []})
        )
        async with _asgi_client(_server) as c:
            resp = await c.post(
                "/",
                headers={**MCP_HEADERS, "Authorization": CALLER_KEY},
                json=_tools_call(),
            )
        assert resp.status_code == 200
        assert upstream.calls[0].request.headers["authorization"] == CALLER_KEY
        # The replayed body reached fastmcp intact: the tool ran and returned the upstream JSON.
        payload = _parse(resp)
        assert json.loads(payload["result"]["content"][0]["text"])["count"] == 3

    @respx.mock
    async def test_bare_get_http_headers_sends_no_authorization(self, _server):
        """The trap the card names: bare get_http_headers() STRIPS Authorization.

        A tool that forgets include={"authorization"} silently sends an anonymous
        upstream request. Registered on the reloaded module so it never leaks
        into other tests.
        """
        from fastmcp.server.dependencies import get_http_headers

        upstream = respx.get(f"{BASE}/probe/").mock(return_value=httpx.Response(200, json={}))

        @_server.mcp.tool
        async def probe_bare() -> str:
            hdrs = get_http_headers()  # bare — no include={"authorization"}
            async with httpx.AsyncClient() as hc:
                await hc.get(f"{BASE}/probe/", headers=hdrs)
            return "ok"

        async with _asgi_client(_server) as c:
            resp = await c.post(
                "/",
                headers={**MCP_HEADERS, "Authorization": CALLER_KEY},
                json=_tools_call("probe_bare"),
            )
        assert resp.status_code == 200
        assert upstream.calls
        assert upstream.calls[0].request.headers.get("authorization") is None

    @respx.mock
    async def test_direct_tool_call_sends_no_server_held_credential(self, _server):
        """Outside an HTTP request there is no caller — and no server credential either."""
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        await _server.list_projects()
        assert upstream.calls[0].request.headers.get("authorization") is None

    @respx.mock
    async def test_upstream_host_is_api_host_when_set(self, _server, monkeypatch):
        """HYDRATA_API_HOST set (prod, via W0.2's env template): Host: hydrata.com goes upstream
        so the internal 127.0.0.1:8081 nginx block matches the right server_name."""
        monkeypatch.setenv("HYDRATA_API_HOST", "hydrata.com")
        reload_server()  # Config.from_env() runs at import
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        async with _asgi_client(_server) as c:
            resp = await c.post(
                "/",
                headers={**MCP_HEADERS, "Authorization": CALLER_KEY},
                json=_tools_call(),
            )
        assert resp.status_code == 200
        assert upstream.calls[0].request.headers["host"] == "hydrata.com"

    @respx.mock
    async def test_upstream_host_is_url_host_when_unset(self, _server):
        """HYDRATA_API_HOST unset (localhost): httpx derives Host from the URL."""
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        async with _asgi_client(_server) as c:
            resp = await c.post(
                "/",
                headers={**MCP_HEADERS, "Authorization": CALLER_KEY},
                json=_tools_call(),
            )
        assert resp.status_code == 200
        assert upstream.calls[0].request.headers["host"] == "hydrata.example.com"

    async def test_non_json_body_does_not_500(self, _server):
        """The middleware replays an unparseable body untouched; fastmcp's own
        -32700 parse error (400) answers it — never a middleware 500, never a 401."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, content=b"not json")
        assert resp.status_code == 400

    async def test_deeply_nested_json_body_does_not_500(self, _server):
        """W0 sweep (epic 2467): a ~100 KB body of '[' makes json.loads raise
        RecursionError, which is NOT a ValueError. The middleware must treat it
        like any other unparseable body — replay untouched so the SDK's own
        -32700 (400) answers — never let it escape as a 500. Reachable anonymously."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, content=b"[" * 100_000)
        assert resp.status_code == 400
        assert _parse(resp)["error"]["code"] == -32700


# ---------------------------------------------------------------------------
# TASK-3181 (W1.0, epic 2467) — the CLI entry serves the SAME ASGI app as prod.
#
# `hydrata-mcp` (pyproject [project.scripts] -> main()) must run uvicorn on the
# module-level `app` that prod's unit file targets (`uvicorn hydrata_mcp.server:app`),
# so there is one server object, one path ("/") and one middleware list. Before
# this card main() called fastmcp's own runner, which built a SECOND app at
# fastmcp's default path (/mcp) — pre-existing since 7586f72 (create_app only).
# ---------------------------------------------------------------------------
class TestCliEntry:
    async def test_main_runs_uvicorn_on_the_module_app(self, _server, monkeypatch):
        import uvicorn

        recorded: dict = {}

        def fake_uvicorn_run(app_obj, **kw):
            recorded["app"] = app_obj
            recorded.update(kw)

        # Patched on the uvicorn MODULE object: the server does `import uvicorn`
        # and calls `uvicorn.run(...)`, so the reloaded module sees the fake.
        monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
        # fastmcp's own runner never calls uvicorn.run (it builds uvicorn.Server
        # itself) — left live it would BIND a port and hang the suite. Stub it so
        # a regression to `mcp.run(...)` fails as an assertion, not a hang.
        monkeypatch.setattr(_server.mcp, "run", lambda *a, **kw: recorded.__setitem__("mcp_run_called", True))
        # Non-default host/port so the forwarding assertions are not tautological
        # (Config's defaults coincide with uvicorn's own).
        monkeypatch.setattr("hydrata_mcp.server.config", _server.Config(api_url=BASE, host="0.0.0.0", port=18765))

        _server.main()

        assert recorded.get("app") is _server.app
        assert recorded.get("host") == "0.0.0.0"
        assert recorded.get("port") == 18765
        assert "mcp_run_called" not in recorded


class TestClientHeaders:
    @respx.mock
    async def test_client_header_and_session_on_every_forwarded_request(self, _server):
        """Two tool calls carrying one client Mcp-Session-Id: every upstream request
        carries X-Hydrata-Client mcp/<version> and the SAME UUID X-Hydrata-Session."""
        import uuid as _uuid

        route = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        session = "6f1c2b9e-3d4a-4b5c-8d6e-7f8091a2b3c4"
        async with _asgi_client(_server) as c:
            for call_id in (1, 2):
                resp = await c.post(
                    "/",
                    headers={**MCP_HEADERS, "Authorization": CALLER_KEY, "mcp-session-id": session},
                    json=_tools_call("list_projects", {}, id=call_id),
                )
                assert resp.status_code == 200, resp.text
        assert route.call_count == 2
        sessions = set()
        for call in route.calls:
            assert call.request.headers["x-hydrata-client"].startswith("mcp/")
            sessions.add(str(_uuid.UUID(call.request.headers["x-hydrata-session"])))
        assert sessions == {session}

    @respx.mock
    async def test_client_header_session_is_a_uuid_without_an_inbound_session(self, _server):
        """stateless_http: no server-issued session; one tool call's requests still share one UUID."""
        import uuid as _uuid

        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        respx.get(f"{BASE}/projects/42/boundaries/").mock(
            return_value=httpx.Response(200, json=[_row(27497, "Boundary 01", gn_layer=1400)])
        )
        respx.patch(f"{BASE}/projects/42/boundaries/27497/").mock(
            return_value=httpx.Response(200, json=_row(27497, "Boundary 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, BOUNDARY_ALTERNATE))
        )
        _mock_layer_summary(BOUNDARY_ALTERNATE)
        _mock_project()
        _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "boundary", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        values = {r.request.headers.get("x-hydrata-session") for r in respx.calls}
        assert len(values) == 1, values
        _uuid.UUID(values.pop())


# ---------------------------------------------------------------------------
# TASK-3204 (W1, epic 3200) — annotations, never-null phase, the list tools.
# Stored proof: -k "annotations or list_inputs or list_time_series or phase".
# ---------------------------------------------------------------------------
READ_TOOLS = {
    "list_projects",
    "suggest_capability",
    "get_project",
    "get_scenario",
    "get_run_status",
    "get_run",
    "list_runs",
    "get_terrain",
    "list_inputs",
    "list_time_series",
    "list_observations",
    "get_observation_comparison",
    "describe_input_kinds",
    "request_input_upload",
    "list_terrain_sources",
}
# TASK-3812: a deleted observation (and its photos) cannot be brought back.
DESTRUCTIVE_TOOLS = {"cancel_run", "delete_observation", "delete_time_series"}


class TestToolAnnotations:
    async def test_annotations_on_all_36_tools_read_hints_and_cancel_destructive(self, _server):
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        assert len(tools) == 36
        for name, tool in tools.items():
            ann = tool.get("annotations") or {}
            assert ann.get("title"), name
            assert ann.get("readOnlyHint") is (name in READ_TOOLS), (name, ann)
            if name not in READ_TOOLS:
                assert ann.get("destructiveHint") is (name in DESTRUCTIVE_TOOLS), (name, ann)
        assert tools["cancel_run"]["annotations"]["destructiveHint"] is True
        assert tools["cancel_run"]["annotations"]["idempotentHint"] is True
        assert tools["start_simulation"]["annotations"]["destructiveHint"] is False


class TestStructuredContent:
    async def test_structured_content_output_schema_is_an_object_on_every_tool(self, _server):
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
        tools = _parse(resp)["result"]["tools"]
        assert len(tools) == len(_server.TOOL_ANNOTATIONS)
        for tool in tools:
            schema = tool.get("outputSchema") or {}
            assert schema.get("type") == "object", (tool["name"], schema)
            assert "x-fastmcp-wrap-result" not in schema, tool["name"]

    @respx.mock
    async def test_structured_content_equals_the_parsed_text_for_every_tool(self, _server):
        # Any upstream call answers with one plausible record; every tool must still
        # come back with structuredContent == json.loads(text) (or a ToolError).
        record = {
            "id": 1,
            "status": "ready",
            "computed_status": "built",
            "base_map": 7,
            "count": 0,
            "next": None,
            "results": [],
        }
        respx.route().mock(return_value=httpx.Response(200, json=record))
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
            tools = _parse(resp)["result"]["tools"]
            structured = []
            for i, tool in enumerate(tools, start=2):
                resp = await c.post(
                    "/",
                    headers={**MCP_HEADERS, "Authorization": CALLER_KEY},
                    json=_tools_call(tool["name"], _schema_args(tool["inputSchema"]), id=i),
                )
                result = _parse(resp)["result"]
                if result.get("isError"):
                    continue  # a refusal is an error string, not a JSON result
                parsed = json.loads(result["content"][0]["text"])
                expected = parsed if isinstance(parsed, dict) else {"result": parsed}
                assert result.get("structuredContent") == expected, tool["name"]
                structured.append(tool["name"])
        # Most tools reach a JSON result on this upstream; the catch-all must not hide them.
        assert len(structured) >= 12, structured

    @respx.mock
    async def test_structured_content_leaves_the_module_functions_returning_text(self, _server):
        respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={"count": 0, "results": []}))
        assert isinstance(await _server.list_projects(), str)


# ---------------------------------------------------------------------------
# TASK-3508 (W2a, epic 3200, D25) — agent keys replace the password on /mcp/.
# TASK-3730 (W2a gate fix, D25 amended 15-10-26) — and ONLY agent keys: a
# tools/call whose Authorization is not ``Bearer hk_…`` (HTTP Basic, a GeoNode
# OAuth Bearer token, anything else) is 401 here, the same shape as an
# anonymous call. No grace period: the Basic sunset machinery is gone. Such a
# caller would bypass the key level, the kill switch, the spend cap/consent
# and the agent activity log. Stored proof: -k agent_key.
# ---------------------------------------------------------------------------
async def _catalog(srv):
    """Every string an agent reads before calling: instructions + tool + parameter text."""
    async with _asgi_client(srv) as c:
        init = await c.post("/", headers=KEY_HEADERS, json=_rpc("initialize", INITIALIZE_PARAMS))
        listed = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list", id=2))
    texts = [_parse(init)["result"].get("instructions") or ""]
    for tool in _parse(listed)["result"]["tools"]:
        texts.append(tool.get("description") or "")
        for prop in (tool.get("inputSchema") or {}).get("properties", {}).values():
            texts.append(prop.get("description") or "")
    return "\n".join(texts)


def _assert_agent_key_401(resp, request_id=1):
    """The anonymous-call refusal shape (TASK-3730 AC1/AC2): 401, Bearer challenge, -32001."""
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == 'Bearer realm="hydrata.com"'
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert body["error"]["code"] == -32001
    message = body["error"]["message"]
    assert "/agents/" in message and "Authorization: Bearer hk_" in message
    assert "password" not in message.lower()
    return message


# Header forms Django's bearer_agent_key (apps/agents/keys.py) does NOT read as
# a key — so neither may this server. Each must be refused before any tool runs.
NOT_AN_AGENT_KEY = [
    pytest.param(CALLER_BASIC, id="basic"),
    pytest.param(CALLER_BASIC_HOLDING_KEY, id="basic-holding-a-key"),
    pytest.param(CALLER_OAUTH, id="oauth-bearer"),
    pytest.param("Bearer", id="bearer-no-token"),
    pytest.param("Bearer hk", id="bearer-hk-no-underscore"),
    pytest.param("Bearer HK_0123456789ab_c2VjcmV0", id="bearer-uppercase-prefix"),
    pytest.param("Bearer x hk_0123456789ab_c2VjcmV0", id="bearer-key-not-first"),
    pytest.param("Token hk_0123456789ab_c2VjcmV0", id="token-scheme"),
    pytest.param("Bearerhk_0123456789ab_c2VjcmV0", id="no-space"),
    pytest.param("Bearer\thk_0123456789ab_c2VjcmV0", id="tab-separator"),
    pytest.param("hk_0123456789ab_c2VjcmV0", id="bare-key-no-scheme"),
]


class TestAgentKeys:
    async def test_agent_key_tool_text_never_asks_for_a_password(self, _server):
        text = await _catalog(_server)
        assert "password" not in text.lower()
        assert "<user>:" not in text
        assert "-u <" not in text
        assert "Authorization: Bearer hk_" in text  # the upload recipe uses the key

    async def test_agent_key_module_source_has_no_password_curl(self, _server):

        source = _server.module_source()
        assert "<password>" not in source
        assert "<user>:" not in source

    @respx.mock
    async def test_agent_key_is_forwarded_verbatim_without_a_note(self, _server):
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(_server, CALLER_KEY)
        assert resp.status_code == 200
        assert upstream.calls[0].request.headers["authorization"] == CALLER_KEY
        result = _parse(resp)["result"]
        assert "auth_deprecation" not in result["structuredContent"]
        assert "auth_deprecation" not in json.loads(result["content"][0]["text"])

    @pytest.mark.parametrize(
        "auth",
        [
            pytest.param(CALLER_KEY, id="canonical"),
            pytest.param("bearer  " + CALLER_KEY.split(" ", 1)[1], id="lowercase-double-space"),
            pytest.param("BEARER " + CALLER_KEY.split(" ", 1)[1], id="uppercase-scheme"),
        ],
    )
    @respx.mock
    async def test_agent_key_scheme_case_and_spacing_forwarded_unchanged(self, _server, auth):
        """AC3: the scheme matches case-insensitively with extra spaces, like Django's
        bearer_agent_key; the header goes upstream exactly as the caller sent it."""
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(_server, auth)
        assert resp.status_code == 200
        assert "error" not in _parse(resp)
        assert upstream.calls[0].request.headers["authorization"] == auth

    @respx.mock
    async def test_agent_key_basic_tools_call_is_401_and_never_forwarded(self, _server):
        """AC1: no grace period — HTTP Basic is refused on the first call."""
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(_server, CALLER_BASIC)
        _assert_agent_key_401(resp)
        assert not upstream.calls

    @respx.mock
    async def test_agent_key_oauth_bearer_tools_call_is_401_and_never_forwarded(self, _server):
        """AC2: a GeoNode OAuth token is not an agent key (gate call 2, until TASK-3512)."""
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(_server, CALLER_OAUTH)
        _assert_agent_key_401(resp)
        assert not upstream.calls

    @pytest.mark.parametrize("auth", NOT_AN_AGENT_KEY)
    @respx.mock
    async def test_agent_key_every_non_key_form_is_401(self, _server, auth):
        upstream = respx.route().mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(_server, auth)
        _assert_agent_key_401(resp)
        assert not upstream.calls

    @pytest.mark.parametrize("auth", [CALLER_BASIC, CALLER_OAUTH], ids=["basic", "oauth-bearer"])
    @respx.mock
    async def test_agent_key_non_key_refused_on_every_tool(self, _server, auth):
        """The refusal is per method, not per tool: a write tool is refused the same way."""
        upstream = respx.route().mock(return_value=httpx.Response(201, json={"id": 1}))
        resp = await _call(_server, auth, "create_project", {"name": "x", "projection": "EPSG:32756"})
        _assert_agent_key_401(resp)
        assert not upstream.calls

    @pytest.mark.parametrize("auth", [CALLER_BASIC, CALLER_OAUTH], ids=["basic", "oauth-bearer"])
    @respx.mock
    async def test_agent_key_batch_with_a_non_key_tools_call_is_401(self, _server, auth):
        upstream = respx.route().mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        async with _asgi_client(_server) as c:
            resp = await c.post(
                "/", headers={**MCP_HEADERS, "Authorization": auth}, json=[_rpc("tools/list", id=1), _tools_call(id=2)]
            )
        _assert_agent_key_401(resp, request_id=2)
        assert not upstream.calls

    @pytest.mark.parametrize(
        "second", [CALLER_BASIC, CALLER_OAUTH, CALLER_KEY], ids=["then-basic", "then-oauth-bearer", "then-the-same-key"]
    )
    @respx.mock
    async def test_agent_key_two_authorization_headers_are_401(self, _server, second):
        """The forwarded header is the LAST Authorization (fastmcp's get_http_headers
        builds a dict), so a key first + Basic second must not pass a first-header
        check and then go upstream as Basic. Any second header is refused."""
        upstream = respx.route().mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        headers = [*MCP_HEADERS.items(), ("Authorization", CALLER_KEY), ("Authorization", second)]
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=headers, json=_tools_call())
        _assert_agent_key_401(resp)
        assert not upstream.calls

    @pytest.mark.parametrize("auth", [CALLER_BASIC, CALLER_OAUTH], ids=["basic", "oauth-bearer"])
    @pytest.mark.parametrize("method,params", EVERY_METHOD)
    async def test_agent_key_catalog_methods_need_a_key_too(self, _server, method, params, auth):
        """TASK-3987 (supersedes AC4 of TASK-3730 / D2 on TASK-2467): initialize /
        tools/list / ping refuse a non-key header like tools/call does, same 401 shape
        (no header at all: test_anon_every_method_is_401_with_challenge)."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers={**MCP_HEADERS, "Authorization": auth}, json=_rpc(method, params))
        _assert_agent_key_401(resp)

    @respx.mock
    async def test_agent_key_error_result_carries_no_auth_note(self, _server):
        respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(403, json={"detail": "no"}))
        result = _parse(await _call(_server, CALLER_KEY))["result"]
        assert result["isError"] is True
        assert "deprecat" not in result["content"][0]["text"].lower()

    async def test_agent_key_no_basic_grace_period_remains(self, _server):
        """AC5: the sunset setting is gone from the code, tests and docs of this repo."""
        import pathlib

        root = pathlib.Path(_server.__file__).resolve().parent.parent
        needles = ("BASIC" + "_SUNSET", "basic_auth" + "_sunset", "_basic" + "_expired", "_basic" + "_deprecation_note")
        files = [
            *root.glob("hydrata_mcp/*.py"),
            *root.glob("tests/*.py"),
            root / "README.md",
            root / "server.json",
            root / "pyproject.toml",
        ]
        hits = [(f.name, n) for f in files for n in needles if n in f.read_text()]
        assert not hits


# ---------------------------------------------------------------------------
# TASK-3766 (W2c, epic 3200) — the tools whose API route Django puts at the
# 'launch' level (apps/agents/middleware.py LAUNCH_URL_NAMES, TASK-3744: a build
# or a terrain import can start AWS Batch) say so in their OWN descriptions, so
# an agent picks the right key up front instead of learning it from a 403
# AGENT_LEVEL_INSUFFICIENT. presign stays at 'draft' on the server (no Terrain
# row, no task), so its description says the IMPORT needs launch, not the presign.
# There is no MCP tool for the bbox terrain fetch (v2-terrain-create-from-bbox);
# get_terrain is a GET (read level) and must NOT claim launch.
# ---------------------------------------------------------------------------
class TestLaunchKeyDescriptions:
    async def _tools(self, srv):
        async with _asgi_client(srv) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
        return {t["name"]: t["description"] for t in _parse(resp)["result"]["tools"]}

    async def test_build_and_finalize_say_they_need_a_launch_key(self, _server):
        tools = await self._tools(_server)
        for name in ("build_scenario", "finalize_terrain_upload"):
            text = tools[name]
            assert "launch-level agent key" in text, name
            assert "AGENT_LEVEL_INSUFFICIENT" in text, name
            assert "/agents/" in text, name

    async def test_presign_says_the_import_needs_launch_not_the_presign(self, _server):
        presign = (await self._tools(_server))["presign_terrain_upload"]
        assert "launch-level agent key" in presign
        assert "AGENT_LEVEL_INSUFFICIENT" in presign
        # The truth on the server: a draft key may presign; finalize and the
        # multipart fallback are the launch-level steps.
        assert "draft key can presign" in presign
        assert "finalize_terrain_upload" in presign and "fallback" in presign

    async def test_read_and_draft_tools_do_not_claim_launch(self, _server):
        """Negative control: the wording is not pasted on every tool — get_terrain is
        a read, create_scenario/attach_input_layer are draft writes."""
        tools = await self._tools(_server)
        for name in ("get_terrain", "create_scenario", "attach_input_layer", "list_inputs"):
            assert "launch-level agent key" not in tools[name], name
