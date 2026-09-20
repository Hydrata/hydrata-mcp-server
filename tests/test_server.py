"""Integration tests for Hydrata MCP server tools.

Uses respx to mock httpx requests so no real API calls are made.
Tests each tool via direct invocation of the async tool functions.
"""

import contextlib
import importlib
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
        monkeypatch.setattr(
            _server.mcp, "run", lambda *a, **kw: recorded.__setitem__("mcp_run_called", True)
        )
        # Non-default host/port so the forwarding assertions are not tautological
        # (Config's defaults coincide with uvicorn's own).
        monkeypatch.setattr(
            _server, "config", _server.Config(api_url=BASE, host="0.0.0.0", port=18765)
        )

        _server.main()

        assert recorded.get("app") is _server.app
        assert recorded.get("host") == "0.0.0.0"
        assert recorded.get("port") == 18765
        assert "mcp_run_called" not in recorded


# ---------------------------------------------------------------------------
# TASK-2469 (W1.1, epic 2467) — project + terrain import tools.
#
# create_project / presign_terrain_upload / finalize_terrain_upload / get_terrain
# wrap the existing V2 REST API. Every case drives the tool through the REAL
# ASGI app with CALLER_BASIC (see _asgi_client above) and asserts the caller's
# Authorization header reached the upstream mock — the pass-through is the
# whole point of the internal-first design. Request BODIES are asserted too:
# the REST contract (api_v2.py upload_presign :1651 / upload_finalize :1756,
# ProjectCreateSerializerV2 name+projection) is what the tools must speak.
# The stored proof selects on `-k "create_project or terrain"`, so every test
# NAME below carries one of those literal tokens (a class name does not match).
# ---------------------------------------------------------------------------
DEM_NAME = "towradgi_dem_1m_ahd.tif"
DEM_SIZE = 31_833_087
STAGING_KEY = f"terrain_uploads/staging/0f4b8c2e-1111-4222-8333-444455556666/{DEM_NAME}"
PROCESS_ID = "9a1b2c3d-4444-4555-8666-777788889999"


async def _call_as_caller(srv, name, arguments):
    """Drive `name` through the ASGI app as CALLER_BASIC; return the JSON-RPC result."""
    async with _asgi_client(srv) as c:
        resp = await c.post(
            "/",
            headers={**MCP_HEADERS, "Authorization": CALLER_BASIC},
            json=_tools_call(name, arguments),
        )
    assert resp.status_code == 200, resp.text
    return _parse(resp)["result"]


def _tool_json(result):
    """The tool's text payload, parsed. Fails loudly on isError so the message shows."""
    assert result.get("isError") is not True, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def _terrain(pk, status, **extra):
    return {"id": pk, "title": "mcp-w1-2469-dem", "status": status, "gn_layer": None, **extra}


class TestCreateProjectTool:
    @respx.mock
    async def test_create_project_posts_name_projection_and_forwards_basic(self, _server):
        route = respx.post(f"{BASE}/projects/").mock(
            return_value=httpx.Response(
                201, json={"id": 42, "name": "mcp-w1-2469-t", "projection": "EPSG:32756"}
            )
        )
        result = await _call_as_caller(
            _server, "create_project", {"name": "mcp-w1-2469-t", "projection": "EPSG:32756"}
        )
        assert route.calls[0].request.headers["authorization"] == CALLER_BASIC
        # Exactly the two writable fields of ProjectCreateSerializerV2.
        assert json.loads(route.calls[0].request.content) == {
            "name": "mcp-w1-2469-t",
            "projection": "EPSG:32756",
        }
        data = _tool_json(result)
        assert data["id"] == 42
        assert data["http_status"] == 201


class TestPresignTerrainUploadTool:
    @respx.mock
    async def test_presign_terrain_upload_posts_filename_type_size_and_forwards_basic(
        self, _server
    ):
        presigned = {
            "process_id": PROCESS_ID,
            "staging_key": STAGING_KEY,
            "upload_url": "https://anuga-test-storage.s3.amazonaws.com/x?X-Amz-Signature=abc",
            "method": "PUT",
            "expires_in": 3600,
            "content_type": "image/tiff",
        }
        route = respx.post(f"{BASE}/projects/42/terrain/upload/presign/").mock(
            return_value=httpx.Response(201, json=presigned)
        )
        result = await _call_as_caller(
            _server,
            "presign_terrain_upload",
            {"project_id": 42, "filename": DEM_NAME, "content_type": "image/tiff", "size": DEM_SIZE},
        )
        assert route.calls[0].request.headers["authorization"] == CALLER_BASIC
        assert json.loads(route.calls[0].request.content) == {
            "filename": DEM_NAME,
            "content_type": "image/tiff",
            "size": DEM_SIZE,
        }
        data = _tool_json(result)
        assert data["upload_url"] == presigned["upload_url"]
        assert data["staging_key"] == STAGING_KEY
        assert data["process_id"] == PROCESS_ID
        assert data["method"] == "PUT"
        assert data["http_status"] == 201

    @respx.mock
    async def test_presign_terrain_upload_defaults_image_tiff_and_omits_unknown_size(
        self, _server
    ):
        """content_type defaults to image/tiff (the signed header the PUT must repeat);
        an unknown size is left OUT of the body — the API treats absent as unchecked."""
        route = respx.post(f"{BASE}/projects/42/terrain/upload/presign/").mock(
            return_value=httpx.Response(201, json={"upload_url": "u", "staging_key": STAGING_KEY})
        )
        await _call_as_caller(
            _server, "presign_terrain_upload", {"project_id": 42, "filename": DEM_NAME}
        )
        assert route.calls[0].request.headers["authorization"] == CALLER_BASIC
        assert json.loads(route.calls[0].request.content) == {
            "filename": DEM_NAME,
            "content_type": "image/tiff",
        }


class TestFinalizeTerrainUploadTool:
    @respx.mock
    async def test_finalize_terrain_upload_posts_key_process_title_and_forwards_basic(
        self, _server
    ):
        route = respx.post(f"{BASE}/projects/42/terrain/upload/finalize/").mock(
            return_value=httpx.Response(202, json=_terrain(7, "creating"))
        )
        result = await _call_as_caller(
            _server,
            "finalize_terrain_upload",
            {
                "project_id": 42,
                "staging_key": STAGING_KEY,
                "process_id": PROCESS_ID,
                "title": "mcp-w1-2469-dem",
            },
        )
        assert route.calls[0].request.headers["authorization"] == CALLER_BASIC
        assert json.loads(route.calls[0].request.content) == {
            "staging_key": STAGING_KEY,
            "process_id": PROCESS_ID,
            "title": "mcp-w1-2469-dem",
        }
        data = _tool_json(result)
        assert data["id"] == 7
        assert data["status"] == "creating"
        assert data["http_status"] == 202

    @respx.mock
    async def test_finalize_terrain_upload_400_surfaces_the_api_detail(self, _server):
        """A finalize against a key whose PUT never landed is a 400 UPLOAD_NOT_FOUND.
        The client used to drop every 4xx body (reason phrase only), so the agent
        saw 'Bad Request' and could not tell a missing PUT from a bad key. The
        error text must now carry the API's error_code/detail."""
        from hydrata_mcp.client import HydrataAPIError

        respx.post(f"{BASE}/projects/42/terrain/upload/finalize/").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error_code": "UPLOAD_NOT_FOUND",
                    "detail": "No uploaded file found for that staging key.",
                },
            )
        )
        with pytest.raises(HydrataAPIError, match="400.*UPLOAD_NOT_FOUND") as exc_info:
            await _server.finalize_terrain_upload(
                project_id=42, staging_key=STAGING_KEY, process_id=PROCESS_ID
            )
        assert "No uploaded file found" in str(exc_info.value)


class TestGetTerrainTool:
    @respx.mock
    async def test_get_terrain_polls_until_ready_and_forwards_basic(self, _server):
        route = respx.get(f"{BASE}/projects/42/terrain/7/").mock(
            side_effect=[
                httpx.Response(200, json=_terrain(7, "creating")),
                httpx.Response(200, json=_terrain(7, "styling")),
                httpx.Response(200, json=_terrain(7, "ready", gn_layer=1234)),
            ]
        )
        result = await _call_as_caller(
            _server,
            "get_terrain",
            {"project_id": 42, "terrain_id": 7, "poll_interval_seconds": 0},
        )
        assert route.call_count == 3
        for call in route.calls:
            assert call.request.headers["authorization"] == CALLER_BASIC
        data = _tool_json(result)
        assert data["outcome"] == "ready"
        assert data["status"] == "ready"
        assert data["polls"] == 3
        assert data["terrain"]["gn_layer"] == 1234

    @respx.mock
    async def test_get_terrain_error_is_terminal_no_further_polls(self, _server):
        route = respx.get(f"{BASE}/projects/42/terrain/7/").mock(
            return_value=httpx.Response(200, json=_terrain(7, "error"))
        )
        data = _tool_json(
            await _call_as_caller(
                _server, "get_terrain", {"project_id": 42, "terrain_id": 7, "poll_interval_seconds": 0}
            )
        )
        assert route.call_count == 1
        assert data["outcome"] == "error"
        assert data["status"] == "error"

    @respx.mock
    async def test_get_terrain_timeout_is_bounded(self, _server):
        """timeout_seconds=0 → exactly ONE poll, then `timed_out` with the LAST status.

        The side effect answers `creating` five times and then a 500, so a
        regression that ignores the bound fails loudly (HydrataAPIError) instead
        of spinning forever under poll_interval_seconds=0.
        """
        calls = {"n": 0}

        def creating_then_500(request):
            calls["n"] += 1
            if calls["n"] > 5:
                return httpx.Response(500)
            return httpx.Response(200, json=_terrain(7, "creating"))

        route = respx.get(f"{BASE}/projects/42/terrain/7/").mock(side_effect=creating_then_500)
        data = _tool_json(
            await _call_as_caller(
                _server,
                "get_terrain",
                {
                    "project_id": 42,
                    "terrain_id": 7,
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0,
                },
            )
        )
        assert route.call_count == 1
        assert data["outcome"] == "timed_out"
        assert data["status"] == "creating"
        assert data["polls"] == 1

    @respx.mock
    async def test_get_terrain_without_id_reads_bare_list_and_picks_newest(self, _server):
        """GET /projects/<id>/terrain/ is a BARE JSON array (no count/results);
        with no terrain_id the tool follows the newest (highest id) row."""
        route = respx.get(f"{BASE}/projects/42/terrain/").mock(
            return_value=httpx.Response(
                200, json=[_terrain(3, "ready"), _terrain(9, "ready"), _terrain(5, "error")]
            )
        )
        data = _tool_json(
            await _call_as_caller(_server, "get_terrain", {"project_id": 42, "poll_interval_seconds": 0})
        )
        assert route.calls[0].request.headers["authorization"] == CALLER_BASIC
        assert data["terrain"]["id"] == 9
        assert data["outcome"] == "ready"
        assert data["terrain_count"] == 3

    @respx.mock
    async def test_get_terrain_empty_list_is_not_found_not_a_loop(self, _server):
        route = respx.get(f"{BASE}/projects/42/terrain/").mock(
            return_value=httpx.Response(200, json=[])
        )
        data = _tool_json(
            await _call_as_caller(_server, "get_terrain", {"project_id": 42, "poll_interval_seconds": 0})
        )
        assert route.call_count == 1
        assert data["outcome"] == "not_found"
        assert data["terrain"] is None

    async def test_terrain_tools_listed_with_cap_and_agent_moves_the_bytes(self, _server):
        """AC3: the descriptions carry the platform cap + 'the agent moves the bytes';
        no tool input is file contents (bytes/base64/path)."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        for name in ("create_project", "presign_terrain_upload", "finalize_terrain_upload", "get_terrain"):
            assert name in tools, name
        presign = tools["presign_terrain_upload"]["description"]
        assert "5°" in presign and "40,000 km²" in presign
        assert "curl" in presign and "--upload-file" in presign
        assert "moves the bytes" in presign
        for name in ("presign_terrain_upload", "finalize_terrain_upload"):
            props = tools[name]["inputSchema"]["properties"]
            for forbidden in ("file", "bytes", "base64", "content", "data", "path"):
                assert forbidden not in props, (name, forbidden)
