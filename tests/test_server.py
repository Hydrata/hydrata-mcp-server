"""Integration tests for Hydrata MCP server tools.

Uses respx to mock httpx requests so no real API calls are made.
Tests each tool via direct invocation of the async tool functions.
"""

import contextlib
import json

import httpx
import pytest
import respx

# Import after conftest sets env vars
pytestmark = pytest.mark.asyncio

BASE = "https://hydrata.example.com/api/v2/anuga"


@pytest.fixture
def _server(env_vars):
    """Import server module after env vars are set (Config.from_env runs at import)."""
    # Reload to pick up test env vars
    import importlib
    import hydrata_mcp.server as srv
    importlib.reload(srv)
    return srv


class TestListProjects:
    @respx.mock
    async def test_returns_json(self, _server):
        respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(200, json={"count": 0, "results": []})
        )
        result = await _server.list_projects()
        data = json.loads(result)
        assert data["count"] == 0
        assert data["results"] == []

    @respx.mock
    async def test_pagination_params(self, _server):
        route = respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(200, json={"count": 0, "results": []})
        )
        await _server.list_projects(page=2, page_size=10)
        assert route.calls[0].request.url.params["page"] == "2"
        assert route.calls[0].request.url.params["page_size"] == "10"


class TestGetProject:
    @respx.mock
    async def test_returns_project(self, _server):
        project = {"id": 1, "name": "Test Project", "projection": "EPSG:28356"}
        respx.get(f"{BASE}/projects/1/").mock(
            return_value=httpx.Response(200, json=project)
        )
        result = await _server.get_project(project_id=1)
        assert json.loads(result)["name"] == "Test Project"

    @respx.mock
    async def test_not_found(self, _server):
        from hydrata_mcp.client import HydrataAPIError
        respx.get(f"{BASE}/projects/999/").mock(return_value=httpx.Response(404))
        with pytest.raises(HydrataAPIError, match="404"):
            await _server.get_project(project_id=999)


class TestGetScenario:
    @respx.mock
    async def test_returns_scenario(self, _server):
        scenario = {"id": 5, "status": "built"}
        respx.get(f"{BASE}/projects/1/scenarios/5/").mock(
            return_value=httpx.Response(200, json=scenario)
        )
        result = await _server.get_scenario(project_id=1, scenario_id=5)
        assert json.loads(result)["status"] == "built"


class TestStartSimulation:
    @respx.mock
    async def test_returns_run_with_status(self, _server):
        respx.post(f"{BASE}/scenarios/5/run/").mock(
            return_value=httpx.Response(202, json={"id": 10, "status": "queued"})
        )
        result = await _server.start_simulation(scenario_id=5)
        data = json.loads(result)
        assert data["http_status"] == 202
        assert data["status"] == "queued"

    @respx.mock
    async def test_non_dict_body(self, _server):
        """Non-dict body is wrapped safely."""
        respx.post(f"{BASE}/scenarios/5/run/").mock(
            return_value=httpx.Response(200, json="accepted")
        )
        result = await _server.start_simulation(scenario_id=5)
        data = json.loads(result)
        assert data["response"] == "accepted"
        assert data["http_status"] == 200


class TestGetRunStatus:
    @respx.mock
    async def test_returns_status(self, _server):
        respx.get(f"{BASE}/runs/10/status/").mock(
            return_value=httpx.Response(200, json={"id": 10, "status": "computing", "progress_pct": 45})
        )
        result = await _server.get_run_status(run_id=10)
        assert json.loads(result)["progress_pct"] == 45


class TestGetRun:
    @respx.mock
    async def test_returns_full_run(self, _server):
        run = {"id": 10, "status": "complete", "duration": 120.5}
        respx.get(f"{BASE}/runs/10/").mock(return_value=httpx.Response(200, json=run))
        result = await _server.get_run(run_id=10)
        assert json.loads(result)["duration"] == 120.5


class TestCancelRun:
    @respx.mock
    async def test_cancel(self, _server):
        respx.post(f"{BASE}/runs/10/cancel/").mock(
            return_value=httpx.Response(200, json={"id": 10, "status": "cancelled"})
        )
        result = await _server.cancel_run(run_id=10)
        data = json.loads(result)
        assert data["status"] == "cancelled"
        assert data["http_status"] == 200

    @respx.mock
    async def test_non_dict_body(self, _server):
        respx.post(f"{BASE}/runs/10/cancel/").mock(
            return_value=httpx.Response(200, json="ok")
        )
        result = await _server.cancel_run(run_id=10)
        data = json.loads(result)
        assert data["response"] == "ok"


class TestRetryRun:
    @respx.mock
    async def test_retry(self, _server):
        respx.post(f"{BASE}/runs/10/retry/").mock(
            return_value=httpx.Response(200, json={"id": 10, "status": "created"})
        )
        result = await _server.retry_run(run_id=10)
        data = json.loads(result)
        assert data["status"] == "created"
        assert data["http_status"] == 200

    @respx.mock
    async def test_non_dict_body(self, _server):
        respx.post(f"{BASE}/runs/10/retry/").mock(
            return_value=httpx.Response(200, json="retrying")
        )
        result = await _server.retry_run(run_id=10)
        data = json.loads(result)
        assert data["response"] == "retrying"


class TestListRuns:
    @respx.mock
    async def test_list_all(self, _server):
        respx.get(f"{BASE}/projects/1/runs/").mock(
            return_value=httpx.Response(200, json={"count": 2, "results": [{"id": 1}, {"id": 2}]})
        )
        result = await _server.list_runs(project_id=1)
        assert json.loads(result)["count"] == 2

    @respx.mock
    async def test_status_filter(self, _server):
        route = respx.get(f"{BASE}/projects/1/runs/").mock(
            return_value=httpx.Response(200, json={"count": 0, "results": []})
        )
        await _server.list_runs(project_id=1, status_filter="error")
        assert route.calls[0].request.url.params["status"] == "error"


# ---------------------------------------------------------------------------
# TASK-3166 (W0.1, epic 2467) — credential pass-through.
#
# These cases drive the REAL ASGI app (lifespan + httpx.ASGITransport) rather
# than calling the tool functions directly, so the auth middleware and the
# fastmcp request context are both exercised. respx mocks only the server's
# OUTBOUND httpx client, so `route.calls[0].request.headers` is exactly what
# went upstream. Every case lives in TestPassthrough so the card's stored
# proof (`pytest -k passthrough`) selects all of them.
# ---------------------------------------------------------------------------
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",  # 406 without both
    "Content-Type": "application/json",
}
# foo:bar — deliberately NOT the env_vars credential (testuser:testpass). httpx
# lets a constructor-level BasicAuth overwrite a per-request Authorization
# header, so a build that still holds a server-side credential could pass the
# verbatim-forward case by coincidence if the test used the same value.
CALLER_BASIC = "Basic Zm9vOmJhcg=="
UPSTREAM_OK = {"count": 0, "results": []}


def _rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


def _tools_call(name="list_projects", arguments=None, id=1):
    return _rpc("tools/call", {"name": name, "arguments": arguments or {}}, id=id)


def _parse(resp):
    """Pull the JSON-RPC payload out of an SSE (200) or plain-JSON (401) response."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):])
        raise AssertionError(f"no data: line in SSE body: {resp.text!r}")
    return resp.json()


@contextlib.asynccontextmanager
async def _asgi_client(srv):
    """Run the app's lifespan (else every request 500s) and yield a client bound to it."""
    async with srv.app.router.lifespan_context(srv.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=srv.app), base_url="http://testserver"
        ) as c:
            yield c


class TestPassthrough:
    """The MCP server holds no identity of its own: it forwards the caller's."""

    @respx.mock
    async def test_anon_tools_call_401_with_basic_challenge(self, _server):
        upstream = respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(200, json=UPSTREAM_OK)
        )
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_tools_call(id=7))
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Basic realm="hydrata.com"'
        body = resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 7
        assert body["error"]["code"] == -32001
        assert "Basic" in body["error"]["message"]
        # Short-circuited in the middleware: nothing reached the upstream API.
        assert not upstream.calls

    @pytest.mark.parametrize(
        "method,params",
        [
            ("tools/list", {}),
            ("ping", {}),
            (
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            ),
        ],
    )
    async def test_anon_catalog_methods_stay_200(self, _server, method, params):
        """Decision D2 (TASK-2467): the catalog is public; only tools/call needs a caller."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_rpc(method, params))
        assert resp.status_code == 200
        assert "error" not in _parse(resp)

    async def test_anon_batch_containing_tools_call_401(self, _server):
        batch = [_rpc("tools/list", id=1), _tools_call(id=2)]
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=batch)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Basic realm="hydrata.com"'
        assert resp.json()["id"] == 2

    @respx.mock
    async def test_basic_header_forwarded_verbatim(self, _server):
        upstream = respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(200, json={"count": 3, "results": []})
        )
        async with _asgi_client(_server) as c:
            resp = await c.post(
                "/",
                headers={**MCP_HEADERS, "Authorization": CALLER_BASIC},
                json=_tools_call(),
            )
        assert resp.status_code == 200
        assert upstream.calls[0].request.headers["authorization"] == CALLER_BASIC
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
                headers={**MCP_HEADERS, "Authorization": CALLER_BASIC},
                json=_tools_call("probe_bare"),
            )
        assert resp.status_code == 200
        assert upstream.calls
        assert upstream.calls[0].request.headers.get("authorization") is None

    @respx.mock
    async def test_direct_tool_call_sends_no_server_held_credential(self, _server):
        """Outside an HTTP request there is no caller — and no server credential either."""
        upstream = respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(200, json=UPSTREAM_OK)
        )
        await _server.list_projects()
        assert upstream.calls[0].request.headers.get("authorization") is None

    @respx.mock
    async def test_upstream_host_is_api_host_when_set(self, _server, monkeypatch):
        """HYDRATA_API_HOST set (prod, via W0.2's env template): Host: hydrata.com goes upstream
        so the internal 127.0.0.1:8081 nginx block matches the right server_name."""
        import importlib

        monkeypatch.setenv("HYDRATA_API_HOST", "hydrata.com")
        importlib.reload(_server)  # Config.from_env() runs at import
        upstream = respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(200, json=UPSTREAM_OK)
        )
        async with _asgi_client(_server) as c:
            resp = await c.post(
                "/",
                headers={**MCP_HEADERS, "Authorization": CALLER_BASIC},
                json=_tools_call(),
            )
        assert resp.status_code == 200
        assert upstream.calls[0].request.headers["host"] == "hydrata.com"

    @respx.mock
    async def test_upstream_host_is_url_host_when_unset(self, _server):
        """HYDRATA_API_HOST unset (localhost): httpx derives Host from the URL."""
        upstream = respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(200, json=UPSTREAM_OK)
        )
        async with _asgi_client(_server) as c:
            resp = await c.post(
                "/",
                headers={**MCP_HEADERS, "Authorization": CALLER_BASIC},
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
