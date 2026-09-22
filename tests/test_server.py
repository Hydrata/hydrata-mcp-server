"""Integration tests for Hydrata MCP server tools.

Uses respx to mock httpx requests so no real API calls are made.
Tests each tool via direct invocation of the async tool functions.
"""

import contextlib
import importlib
import json
import time

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
        scenario = {"id": 5, "computed_status": "built"}
        respx.get(f"{BASE}/projects/1/scenarios/5/").mock(
            return_value=httpx.Response(200, json=scenario)
        )
        result = await _server.get_scenario(project_id=1, scenario_id=5)
        assert json.loads(result)["computed_status"] == "built"


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
    async def test_get_terrain_sleep_is_capped_by_the_remaining_timeout(self, _server):
        """A poll interval LONGER than the timeout must not overshoot the bound.

        W1a sweep (epic 2467): the docstring promises "bounded by
        timeout_seconds", but an uncapped `asyncio.sleep(poll_interval_seconds)`
        made the wall clock ~interval when interval > timeout (interval 600 →
        one 600 s sleep → prod's 300 s /mcp/ proxy cuts the call and the agent
        gets nothing back). The sleep is now min(interval, remaining). This is
        a REAL 1 s wait on purpose: patching asyncio.sleep or time.monotonic
        would also patch the event loop's clock.
        """
        route = respx.get(f"{BASE}/projects/42/terrain/7/").mock(
            return_value=httpx.Response(200, json=_terrain(7, "creating"))
        )
        started = time.monotonic()
        data = _tool_json(
            await _call_as_caller(
                _server,
                "get_terrain",
                {
                    "project_id": 42,
                    "terrain_id": 7,
                    "timeout_seconds": 1,
                    "poll_interval_seconds": 60,
                },
            )
        )
        wall = time.monotonic() - started
        assert wall < 5, f"sleep was not capped by the timeout: {wall:.1f}s"
        assert data["outcome"] == "timed_out"
        assert data["status"] == "creating"
        # It DID sleep and re-poll (not an early return), just not for 60 s.
        assert route.call_count >= 2
        assert data["polls"] == route.call_count
        assert data["elapsed_seconds"] < 5

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


# ---------------------------------------------------------------------------
# TASK-3171 (W1.2, epic 2467) — create_time_series + attach_input_layer.
#
# create_time_series POSTs /projects/<id>/time-series/ with `series_type` and
# `units` as TOP-LEVEL keys (TimeSeries HAS both fields: hydrology/models.py:90-96;
# the Hydrographs panel filters the list by ?series_type, so a folded-into-
# description value would leave an inflow series invisible) and mirrors
# TimeSeries.clean() client-side, because a bad row is an UNHANDLED 500 on the
# API (full_clean's ValidationError is not an APIException), whose body the
# client masks. attach_input_layer PATCHes the project's DEFAULT row for all
# SIX kinds — the terrain chain seeds Structure 01 / MeshRegion 01 too, and a
# POST to /structures/ or /mesh-regions/ with gn_layer is silently overwritten
# by the async layer factory (api_v2.py perform_create :5247-5261) — so the
# suite registers those POST routes and asserts they are NEVER called. The
# execution-status route is GeoNode's, outside /api/v2/anuga, so its URL is
# asserted at the ORIGIN. The stored proof selects on
# `-k "time_series or attach_input_layer"`: every test NAME carries a token.
# ---------------------------------------------------------------------------
ORIGIN = "https://hydrata.example.com"
EXEC_ID = "b7e1c2d3-4444-4555-8666-777788889999"
EXEC_STATUS_URL = f"{ORIGIN}/api/v2/resource-service/execution-status/{EXEC_ID}"
EXEC_REQUEST_URL = f"{ORIGIN}/api/v2/executionrequest/{EXEC_ID}/"
INPUT_LAYER_ROUTES = {
    "boundary": "boundaries",
    "friction": "frictions",
    "inflow": "inflows",
    "rainfall": "rainfalls",
    "structure": "structures",
    "mesh_region": "mesh-regions",
}
DEFAULT_TITLES = {
    "boundary": "Boundary 01",
    "friction": "Friction 01",
    "inflow": "Inflow 01",
    "rainfall": "Rainfall 01",
    "structure": "Structure 01",
    "mesh_region": "MeshRegion 01",
}
ROW_DATA = [
    {"timestamp": "1998-08-17T00:00:00", "value": 0.0},
    {"timestamp": "1998-08-17T00:05:00Z", "value": 1.5},  # clean() strips a trailing Z
    {"timestamp": "1998-08-17T00:10:00", "value": "2.25"},  # clean() float()s a numeric string
]


def _series_args(**overrides):
    args = {
        "project_id": 42,
        "name": "rain_gauge_200",
        "data": {"rowData": ROW_DATA},
        "series_type": "hyetograph",
        "units": "mm/hr",
        "timezone": "Australia/Sydney",
    }
    args.update(overrides)
    return args


def _series_row(pk, **overrides):
    row = {
        "id": pk,
        "project": 42,
        "name": "rain_gauge_200",
        "source": "",
        "description": "",
        "location_name": "",
        "timezone": "Australia/Sydney",
        "series_type": "hyetograph",
        "units": "mm/hr",
        "data": {"rowData": ROW_DATA},
        "perms": ["view", "change"],
    }
    row.update(overrides)
    return row


def _exec(status, resources=None, **extra):
    return {
        "user": "testuser",
        "status": status,
        "func_name": "import_new_resource",
        "output_params": {"resources": resources or []},
        "log": None,
        **extra,
    }


def _row(pk, title, gn_layer=None):
    return {"id": pk, "title": title, "project": 42, "gn_layer": gn_layer, "gn_layer_name": None, "perms": []}


def _dataset(pk, alternate):
    return {"dataset": {"pk": str(pk), "alternate": alternate, "title": alternate}}


def _mock_no_wrapper_posts():
    """The two routes attach_input_layer must NEVER hit (see the block comment)."""
    return (
        respx.post(f"{BASE}/projects/42/structures/").mock(return_value=httpx.Response(201, json={})),
        respx.post(f"{BASE}/projects/42/mesh-regions/").mock(return_value=httpx.Response(201, json={})),
    )


def _tool_error_text(result):
    """The refusal text of an isError result. Fails loudly if the tool did NOT refuse."""
    assert result.get("isError") is True, result["content"][0]["text"]
    return result["content"][0]["text"]


class TestCreateTimeSeriesTool:
    @respx.mock
    async def test_create_time_series_posts_series_type_and_units_top_level_and_forwards_basic(
        self, _server
    ):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        result = await _call_as_caller(_server, "create_time_series", _series_args())
        assert route.calls[0].request.headers["authorization"] == CALLER_BASIC
        body = json.loads(route.calls[0].request.content)
        # AC (flipped at the re-aim): both are TOP-LEVEL body keys, never folded
        # into description; rows travel VERBATIM (clean() normalises them server-side).
        assert body == {
            "name": "rain_gauge_200",
            "description": "",
            "source": "",
            "location_name": "",
            "timezone": "Australia/Sydney",
            "series_type": "hyetograph",
            "units": "mm/hr",
            "data": {"rowData": ROW_DATA},
        }
        assert "series_type" not in body["description"]
        data = _tool_json(result)
        # Compact record: the API echoes the whole row incl. `data` (~100 KB for
        # a 1447-row gauge); 50 of those would drown the driving agent's context.
        assert data == {
            "id": 77,
            "name": "rain_gauge_200",
            "series_type": "hyetograph",
            "units": "mm/hr",
            "timezone": "Australia/Sydney",
            "row_count": 3,
            "http_status": 201,
        }
        assert "data" not in data

    @respx.mock
    async def test_create_time_series_sends_stage_series_type_verbatim(self, _server):
        """The tide series in the bundle is `stage`; a build that drops the key
        would still land as the model default (hyetograph) and grade green."""
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(78, series_type="stage", units="m"))
        )
        data = _tool_json(
            await _call_as_caller(
                _server, "create_time_series", _series_args(series_type="stage", units="m")
            )
        )
        body = json.loads(route.calls[0].request.content)
        assert body["series_type"] == "stage"
        assert body["units"] == "m"
        assert data["series_type"] == "stage"

    @respx.mock
    async def test_create_time_series_rejects_unknown_series_type_without_upstream_call(
        self, _server
    ):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        text = _tool_error_text(
            await _call_as_caller(_server, "create_time_series", _series_args(series_type="rainfall"))
        )
        assert not route.called
        for choice in ("hyetograph", "hydrograph", "stage", "generic"):
            assert choice in text

    @pytest.mark.parametrize(
        "bad_data",
        [
            [{"timestamp": "1998-08-17T00:00:00", "value": 1}],  # a bare list (model default!)
            {"rows": []},  # no rowData
            {"rowData": {"timestamp": "1998-08-17T00:00:00", "value": 1}},  # rowData not a list
            "not even json",
        ],
        ids=["list", "no-rowData", "rowData-not-list", "string"],
    )
    @respx.mock
    async def test_create_time_series_rejects_non_rowdata_shape_without_upstream_call(
        self, _server, bad_data
    ):
        """A non-{rowData: [...]} `data` is an UNHANDLED 500 on the API (clean()
        does data.get('rowData') without a guard) whose body the client masks."""
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        text = _tool_error_text(
            await _call_as_caller(_server, "create_time_series", _series_args(data=bad_data))
        )
        assert not route.called
        # A non-object is refused one layer earlier, by the tool's own input
        # schema (`data: dict` → pydantic "Input should be a valid dictionary");
        # an object of the wrong shape by _validate_row_data. Both: no request.
        assert "rowData" in text or "valid dictionary" in text

    @pytest.mark.parametrize(
        "bad_row",
        [
            {"timestamp": "17/08/1998 00:00", "value": 1.0},  # not ISO 8601
            {"timestamp": "1998-08-17T00:00:00", "value": "heavy"},  # not a number
            {"timestamp": "1998-08-17T00:00:00"},  # value missing
            {"value": 1.0},  # timestamp missing
            {"timestamp": 19980817, "value": 1.0},  # timestamp not a string
            ["1998-08-17T00:00:00", 1.0],  # row not a dict
        ],
        ids=["bad-iso", "non-numeric", "no-value", "no-timestamp", "int-timestamp", "row-list"],
    )
    @respx.mock
    async def test_create_time_series_rejects_bad_row_without_upstream_call(
        self, _server, bad_row
    ):
        """Mirrors TimeSeries.clean() (hydrology/models.py:126-151): its
        ValidationError is raised from full_clean() inside save(), which DRF
        does NOT map to a 400 — the API answers 500 and the client hides the
        body. The tool must refuse first, naming the offending row."""
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        text = _tool_error_text(
            await _call_as_caller(
                _server,
                "create_time_series",
                _series_args(data={"rowData": [ROW_DATA[0], bad_row]}),
            )
        )
        assert not route.called
        assert "row 1" in text

    @respx.mock
    async def test_create_time_series_400_surfaces_the_api_detail(self, _server):
        """A bad `timezone` IS a 400 (DRF ChoiceField) — its detail must reach the agent."""
        from hydrata_mcp.client import HydrataAPIError

        respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(
                400, json={"timezone": ['"Mars/Olympus" is not a valid choice.']}
            )
        )
        with pytest.raises(HydrataAPIError, match="400") as exc_info:
            await _server.create_time_series(
                **{k: v for k, v in _series_args(timezone="Mars/Olympus").items()}
            )
        assert "Mars/Olympus" in str(exc_info.value)


class TestAttachInputLayerTool:
    @pytest.mark.parametrize("kind", list(INPUT_LAYER_ROUTES))
    @respx.mock
    async def test_attach_input_layer_patches_the_default_row_and_forwards_basic(
        self, _server, kind
    ):
        route_name = INPUT_LAYER_ROUTES[kind]
        pk = 124_556
        # The execution-status route is GeoNode's, at the ORIGIN — not under /api/v2/anuga.
        status_route = respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}]))
        )
        list_route = respx.get(f"{BASE}/projects/42/{route_name}/").mock(
            return_value=httpx.Response(200, json=[_row(pk, DEFAULT_TITLES[kind], gn_layer=1400)])
        )
        patch_route = respx.patch(f"{BASE}/projects/42/{route_name}/{pk}/").mock(
            return_value=httpx.Response(200, json=_row(pk, DEFAULT_TITLES[kind], gn_layer=1502))
        )
        dataset_route = respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:rai_42_rainfall_01"))
        )
        structures_post, mesh_regions_post = _mock_no_wrapper_posts()

        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": kind, "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )

        for route in (status_route, list_route, patch_route, dataset_route):
            assert route.called, route
            assert route.calls[0].request.headers["authorization"] == CALLER_BASIC
        assert str(status_route.calls[0].request.url) == EXEC_STATUS_URL
        # The PATCH: plural route + the default row's pk, body exactly {"gn_layer": pk}.
        assert json.loads(patch_route.calls[0].request.content) == {"gn_layer": 1502}
        # NEVER a POST to the two wrapper routes (the async layer factory would
        # overwrite gn_layer moments after the 201).
        assert not structures_post.called
        assert not mesh_regions_post.called
        assert data["outcome"] == "attached"
        assert data["kind"] == kind
        assert data["route"] == route_name
        assert data["row_id"] == pk
        assert data["row_title"] == DEFAULT_TITLES[kind]
        assert data["row_ids"] == [pk]
        assert data["gn_layer"] == 1502
        assert data["dataset_pk"] == 1502
        assert data["dataset_alternate"] == "geonode:rai_42_rainfall_01"
        assert data["execution_status"] == "finished"
        assert data["http_status"] == 200

    @pytest.mark.parametrize("kind", ["breakline", "culvert"])
    @respx.mock
    async def test_attach_input_layer_refuses_breakline_and_culvert_without_any_http(
        self, _server, kind
    ):
        status_route = respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}]))
        )
        list_routes = [
            respx.get(f"{BASE}/projects/42/{r}/").mock(return_value=httpx.Response(200, json=[]))
            for r in INPUT_LAYER_ROUTES.values()
        ]
        structures_post, mesh_regions_post = _mock_no_wrapper_posts()
        text = _tool_error_text(
            await _call_as_caller(
                _server, "attach_input_layer", {"project_id": 42, "kind": kind, "execution_id": EXEC_ID}
            )
        )
        assert not status_route.called
        assert not any(r.called for r in list_routes)
        assert not structures_post.called and not mesh_regions_post.called
        assert "TASK-3040" in text
        assert "not conveyed" in text
        assert kind in text

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../../api/v2/users/",  # dot-segments: would reach another route at the origin
            "b7e1c2d3-4444-4555-8666-77778888999",  # one hex digit short
            f"{EXEC_ID}?format=json",  # query injection
            "",
        ],
        ids=["traversal", "short", "query", "empty"],
    )
    @respx.mock
    async def test_attach_input_layer_refuses_a_non_uuid_execution_id_without_any_http(
        self, _server, bad_id
    ):
        """W1a sweep (epic 2467): execution_id is the ONE free-form path segment a
        tool interpolates into a URL, and on prod that URL is the unthrottled
        loopback door to Django (geonode-https.j2 :8081). GeoNode's exec_id is a
        UUIDField, so anything else is refused before a request is built."""
        catch_all = respx.route().mock(return_value=httpx.Response(200, json={}))
        text = _tool_error_text(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                # timeout 0 so an un-refused id fails fast as `timed_out`, not a hang
                {"project_id": 42, "kind": "rainfall", "execution_id": bad_id, "timeout_seconds": 0},
            )
        )
        assert not catch_all.called
        assert "UUID" in text

    @respx.mock
    async def test_attach_input_layer_refuses_unknown_kind_listing_the_six(self, _server):
        status_route = respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}]))
        )
        text = _tool_error_text(
            await _call_as_caller(
                _server, "attach_input_layer", {"project_id": 42, "kind": "terrain", "execution_id": EXEC_ID}
            )
        )
        assert not status_route.called
        for kind in INPUT_LAYER_ROUTES:
            assert kind in text

    @respx.mock
    async def test_attach_input_layer_surfaces_execution_failure_verbatim(self, _server):
        failed = _exec(
            "failed",
            log="Unable to import the file: rainfall.geojson is not a valid GeoJSON",
            step="import",
        )
        status_route = respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=failed))
        list_route = respx.get(f"{BASE}/projects/42/rainfalls/").mock(
            return_value=httpx.Response(200, json=[_row(1, "Rainfall 01")])
        )
        patch_route = respx.patch(f"{BASE}/projects/42/rainfalls/1/").mock(
            return_value=httpx.Response(200, json={})
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "rainfall", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert status_route.call_count == 1  # failed is terminal: no further polls
        assert not list_route.called
        assert not patch_route.called
        assert data["outcome"] == "upload_failed"
        assert data["execution_status"] == "failed"
        assert data["execution"] == failed  # verbatim
        assert "not a valid GeoJSON" in data["execution"]["log"]

    @respx.mock
    async def test_attach_input_layer_falls_back_to_executionrequest_for_the_dataset_pk(
        self, _server
    ):
        """finished but output_params.resources empty → GET /api/v2/executionrequest/<id>/
        → request.output_params.resources[0].id (the e2e recipe's fallback)."""
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [])))
        fallback = respx.get(EXEC_REQUEST_URL).mock(
            return_value=httpx.Response(
                200, json={"request": {"output_params": {"resources": [{"id": "1502"}]}}}
            )
        )
        respx.get(f"{BASE}/projects/42/boundaries/").mock(
            return_value=httpx.Response(200, json=[_row(27497, "Boundary 01")])
        )
        patch_route = respx.patch(f"{BASE}/projects/42/boundaries/27497/").mock(
            return_value=httpx.Response(200, json=_row(27497, "Boundary 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:boundary"))
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "boundary", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert fallback.called
        assert fallback.calls[0].request.headers["authorization"] == CALLER_BASIC
        assert json.loads(patch_route.calls[0].request.content) == {"gn_layer": 1502}
        assert data["outcome"] == "attached"
        assert data["dataset_pk"] == 1502

    @respx.mock
    async def test_attach_input_layer_polls_ready_running_then_finished(self, _server):
        status_route = respx.get(EXEC_STATUS_URL).mock(
            side_effect=[
                httpx.Response(200, json=_exec("ready")),
                httpx.Response(200, json=_exec("running")),
                httpx.Response(200, json=_exec("finished", [{"id": 1502}])),
            ]
        )
        respx.get(f"{BASE}/projects/42/frictions/").mock(
            return_value=httpx.Response(200, json=[_row(5, "Friction 01")])
        )
        respx.patch(f"{BASE}/projects/42/frictions/5/").mock(
            return_value=httpx.Response(200, json=_row(5, "Friction 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:friction"))
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "friction", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert status_route.call_count == 3
        assert data["polls"] == 3
        assert data["outcome"] == "attached"

    @respx.mock
    async def test_attach_input_layer_timeout_is_bounded_and_patches_nothing(self, _server):
        """timeout_seconds=0 → ONE status read, then `timed_out`; the agent re-calls."""
        status_route = respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("running"))
        )
        list_route = respx.get(f"{BASE}/projects/42/rainfalls/").mock(
            return_value=httpx.Response(200, json=[_row(1, "Rainfall 01")])
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {
                    "project_id": 42,
                    "kind": "rainfall",
                    "execution_id": EXEC_ID,
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0,
                },
            )
        )
        assert status_route.call_count == 1
        assert not list_route.called
        assert data["outcome"] == "timed_out"
        assert data["execution_status"] == "running"
        assert data["polls"] == 1

    @respx.mock
    async def test_attach_input_layer_with_no_default_row_says_run_finalize_first(self, _server):
        respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}]))
        )
        respx.get(f"{BASE}/projects/42/inflows/").mock(return_value=httpx.Response(200, json=[]))
        patch_route = respx.patch(url__regex=rf"{BASE}/projects/42/inflows/\d+/").mock(
            return_value=httpx.Response(200, json={})
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "inflow", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert not patch_route.called
        assert data["outcome"] == "no_default_row"
        assert "finalize_terrain_upload" in data["message"]
        assert data["dataset_pk"] == 1502  # the upload DID land; only the row is missing

    @respx.mock
    async def test_attach_input_layer_with_several_rows_patches_the_01_row_and_reports_ids(
        self, _server
    ):
        """'First' is deterministic: the '<Kind> 01' row if present, else the lowest id
        (the list has no declared ordering). Every id is reported."""
        respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}]))
        )
        respx.get(f"{BASE}/projects/42/mesh-regions/").mock(
            return_value=httpx.Response(
                200,
                json=[_row(9, "MeshRegion 02"), _row(5, "MeshRegion 01"), _row(2, "Fine mesh")],
            )
        )
        patch_route = respx.patch(f"{BASE}/projects/42/mesh-regions/5/").mock(
            return_value=httpx.Response(200, json=_row(5, "MeshRegion 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:mesh"))
        )
        _mock_no_wrapper_posts()
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "mesh_region", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert patch_route.called
        assert data["row_id"] == 5
        assert data["row_ids"] == [9, 5, 2]

    async def test_attach_input_layer_and_time_series_listed_not_conveyed_no_file_inputs(
        self, _server
    ):
        """AC3: 'not conveyed' is in attach_input_layer's description; neither
        tool takes file contents (create_time_series takes a small JSON `data`
        object by design — decision D7 forbids bytes, not row objects)."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        assert "create_time_series" in tools and "attach_input_layer" in tools
        attach = tools["attach_input_layer"]["description"]
        assert "not conveyed" in attach
        assert "TASK-3040" in attach
        assert "base_file" in attach  # the curl recipe the agent runs itself
        for name in ("create_time_series", "attach_input_layer"):
            props = tools[name]["inputSchema"]["properties"]
            for forbidden in ("file", "bytes", "base64", "content", "path", "geojson"):
                assert forbidden not in props, (name, forbidden)
        series_props = tools["create_time_series"]["inputSchema"]["properties"]
        assert "series_type" in series_props and "units" in series_props


# ---------------------------------------------------------------------------
# TASK-3172 (W1.3, epic 2467) — create_scenario + build_scenario.
#
# create_scenario POSTs the WRITE serializer's fields (ScenarioCreateSerializerV2,
# serializers_v2.py:819 — its response carries NO estimate) and then GETs the
# detail, whose READ serializer carries mesh_triangle_count_estimate (+ the
# _breakdown) and `computed_status` — NEVER `status`: the round-2 red-team found
# the pre-existing get_scenario test mocking a key the API does not return.
# build_scenario shows the number before it spends: the re-call checks on
# latest_run come FIRST (a re-POST after `built` dispatches a NEW Run,
# services.py:1042-1048; the dedup 409 blocks only in-flight + created), then
# the boundary / resolution / 100k-confirm gates, then POST /build/ with
# raise_for_status=False so a 422 MESH_TOO_LARGE (or a 409) body reaches the
# agent VERBATIM as a non-error result, then a bounded poll of the detail's
# computed_status. Every case asserts whether /build/ was hit. The stored proof
# selects on `-k "create_scenario or build_scenario"`: every test NAME carries
# one of those tokens.
# ---------------------------------------------------------------------------
SCENARIO_URL = f"{BASE}/projects/42/scenarios/5/"
BUILD_URL = f"{BASE}/projects/42/scenarios/5/build/"
BOUNDARY_URL = f"{BASE}/projects/42/boundaries/27497/"
ESTIMATE_BREAKDOWN = {"base": 19_729, "mesh_regions": 0, "structures": 0, "total": 19_729}
MESH_TOO_LARGE = {
    "error_code": "MESH_TOO_LARGE",
    "estimate": 25_568_180,
    "ceiling": 7_737_436,
    "detail": (
        "This mesh would be ~25,568,180 triangles, above the 7,737,436-triangle "
        "limit. Coarsen the resolution or reduce the extent."
    ),
}
DEDUP_409 = {"status": "building", "run_id": 501, "detail": "A build is already in progress for this scenario."}


def _run(pk, status, **extra):
    return {
        "id": pk,
        "project": 42,
        "scenario": 5,
        "status": status,
        "error_message": None,
        "user_message": None,
        "status_detail": None,
        "mesh_triangle_count": None,
        "log": "…the full build log…",
        **extra,
    }


def _scenario(computed_status="created", estimate=19_729, latest_run=None, **overrides):
    """A member's ScenarioSerializerV2 detail: boundary + computed_status present."""
    row = {
        "id": 5,
        "project": 42,
        "name": "mcp-w1-3172-t",
        "description": "",
        "boundary": 27497,
        "terrain": 110533,
        "friction": 124557,
        "inflow": 124555,
        "rainfall": 124556,
        "structure": 124558,
        "mesh_region": None,
        "network": None,
        "resolution": 36.0,
        "duration": 43200,
        "computed_status": computed_status,
        "latest_run": latest_run,
        "latest_complete_run": None,
        "mesh_triangle_count_estimate": estimate,
        "mesh_triangle_count_estimate_breakdown": (
            None if estimate is None else {**ESTIMATE_BREAKDOWN, "total": estimate}
        ),
        "latest_run_is_valid": None,
        "perms": ["view", "change"],
    }
    row.update(overrides)
    return row


def _mock_boundary(has_features=True):
    return respx.get(BOUNDARY_URL).mock(
        return_value=httpx.Response(
            200,
            json={"id": 27497, "title": "Boundary 01", "project": 42, "gn_layer": 1524, "has_features": has_features},
        )
    )


def _mock_build(status=202, body=None):
    body = {"status": "created", "run_id": 501, "scenario_id": 5} if body is None else body
    return respx.post(BUILD_URL).mock(return_value=httpx.Response(status, json=body))


def _detail_to_built(first_detail):
    """Detail GETs: the pre-check read, then the poll sees created → building → built."""
    return respx.get(SCENARIO_URL).mock(
        side_effect=[
            httpx.Response(200, json=first_detail),
            httpx.Response(200, json=_scenario("created", latest_run=_run(501, "created"))),
            httpx.Response(200, json=_scenario("building", latest_run=_run(501, "building"))),
            httpx.Response(
                200,
                json=_scenario(
                    "built",
                    latest_run=_run(501, "built", mesh_triangle_count=19_812),
                    latest_run_is_valid=True,
                ),
            ),
        ]
    )


async def _build(srv, **overrides):
    args = {"project_id": 42, "scenario_id": 5, "poll_interval_seconds": 0}
    args.update(overrides)
    return await _call_as_caller(srv, "build_scenario", args)


class TestCreateScenarioTool:
    @respx.mock
    async def test_create_scenario_posts_the_write_fields_and_reports_the_estimate_from_the_detail_get(
        self, _server
    ):
        created = {
            "id": 5, "name": "mcp-w1-3172-t", "description": "", "terrain": 110533,
            "boundary": 27497, "friction": 124557, "inflow": 124555, "rainfall": 124556,
            "structure": 124558, "mesh_region": None, "network": None,
            "resolution": 36.0, "duration": 43200,
        }
        post_route = respx.post(f"{BASE}/projects/42/scenarios/").mock(
            return_value=httpx.Response(201, json=created)
        )
        detail_route = respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        result = await _call_as_caller(
            _server,
            "create_scenario",
            {
                "project_id": 42, "name": "mcp-w1-3172-t", "resolution": 36, "duration": 43200,
                "terrain": 110533, "boundary": 27497, "friction": 124557, "inflow": 124555,
                "rainfall": 124556, "structure": 124558,
            },
        )
        assert post_route.calls[0].request.headers["authorization"] == CALLER_BASIC
        assert detail_route.calls[0].request.headers["authorization"] == CALLER_BASIC
        # Exactly ScenarioCreateSerializerV2's writable fields the tool exposes
        # (network is not offered); an unset FK travels as null.
        assert json.loads(post_route.calls[0].request.content) == {
            "name": "mcp-w1-3172-t", "description": "", "resolution": 36, "duration": 43200,
            "terrain": 110533, "boundary": 27497, "friction": 124557, "inflow": 124555,
            "rainfall": 124556, "structure": 124558, "mesh_region": None,
        }
        data = _tool_json(result)
        assert data["id"] == 5
        assert data["http_status"] == 201
        # The estimate comes from the DETAIL — the create response has none.
        assert "mesh_triangle_count_estimate" not in created
        assert data["mesh_triangle_count_estimate"] == 19_729
        assert data["mesh_triangle_count_estimate_breakdown"]["total"] == 19_729
        assert data["computed_status"] == "created"
        assert data["mesh_region"] is None
        assert "latest_run" not in data and "perms" not in data


class TestBuildScenarioTool:
    @respx.mock
    async def test_build_scenario_under_cap_posts_build_then_polls_computed_status_to_built(self, _server):
        detail_route = _detail_to_built(_scenario())
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert build_route.calls[0].request.headers["authorization"] == CALLER_BASIC
        assert boundary_route.calls[0].request.headers["authorization"] == CALLER_BASIC
        for call in detail_route.calls:
            assert call.request.headers["authorization"] == CALLER_BASIC
        assert detail_route.call_count == 4  # pre-check, then created → building → built
        assert data["outcome"] == "built"
        assert data["computed_status"] == "built"
        assert data["posted"] is True
        assert data["http_status"] == 202
        assert data["build"] == {"status": "created", "run_id": 501, "scenario_id": 5}
        assert data["run_id"] == 501
        assert data["run_status"] == "built"
        assert data["mesh_triangle_count"] == 19_812
        assert data["mesh_triangle_count_estimate"] == 19_729
        assert data["polls"] == 3
        # Compact: never the whole detail (latest_run carries the full build log).
        assert "latest_run" not in data and "log" not in data

    @respx.mock
    async def test_build_scenario_over_100k_without_confirm_refuses_and_never_posts(self, _server):
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(estimate=255_412)))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called
        assert data["outcome"] == "refused"
        assert data["posted"] is False
        assert data["mesh_triangle_count_estimate"] == 255_412
        assert "255,412" in data["reason"] and "confirm=true" in data["reason"]

    @respx.mock
    async def test_build_scenario_over_100k_with_confirm_true_posts(self, _server):
        _detail_to_built(_scenario(estimate=255_412))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server, confirm=True))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"

    @respx.mock
    async def test_build_scenario_refuses_a_null_boundary_without_posting(self, _server):
        respx.get(SCENARIO_URL).mock(
            return_value=httpx.Response(200, json=_scenario(boundary=None, estimate=0))
        )
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called and not boundary_route.called
        assert data["outcome"] == "refused"
        assert "boundary has no features" in data["reason"]

    @respx.mock
    async def test_build_scenario_refuses_a_boundary_without_features_without_posting(self, _server):
        """The default 'Boundary 01' row before attach_input_layer: the server ADMITS
        the build and errors at make_package — refuse here, naming the row."""
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(estimate=0)))
        boundary_route = _mock_boundary(False)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert boundary_route.called
        assert not build_route.called
        assert data["outcome"] == "refused"
        assert "boundary has no features" in data["reason"] and "27497" in data["reason"]

    @respx.mock
    async def test_build_scenario_estimate_zero_with_features_proceeds_to_post(self, _server):
        """Estimate 0 is NOT 'no boundary': int(round(...)) of a coarse resolution over
        a real boundary is 0 and BUILDS (localhost scenario 86955: 8 triangles)."""
        _detail_to_built(_scenario(estimate=0))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"

    @respx.mock
    async def test_build_scenario_refuses_when_the_estimate_is_none_without_posting(self, _server):
        """resolution 0 (FloatField default 100, not nullable) → estimate None."""
        respx.get(SCENARIO_URL).mock(
            return_value=httpx.Response(200, json=_scenario(estimate=None, resolution=0.0))
        )
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called
        assert data["outcome"] == "refused"
        assert "resolution is 0/unset" in data["reason"]

    @respx.mock
    async def test_build_scenario_returns_the_422_mesh_too_large_body_verbatim_as_a_non_error(
        self, _server
    ):
        detail_route = respx.get(SCENARIO_URL).mock(
            return_value=httpx.Response(200, json=_scenario(estimate=25_568_180, resolution=1.0))
        )
        _mock_boundary(True)
        build_route = _mock_build(422, MESH_TOO_LARGE)
        result = await _build(_server, confirm=True)
        assert result.get("isError") is not True, result["content"][0]["text"]
        data = _tool_json(result)
        assert build_route.call_count == 1
        assert data == {**MESH_TOO_LARGE, "http_status": 422}
        assert detail_route.call_count == 1  # nothing to poll: no Run was created

    @respx.mock
    async def test_build_scenario_returns_a_409_with_error_code_verbatim_and_does_not_poll(self, _server):
        unavailable = {
            "error_code": "COMPUTE_TARGET_UNAVAILABLE",
            "detail": "ANUGA_BATCH_GPU_L40S_MEMORY_LIMIT_MIB is not a positive integer",
        }
        detail_route = respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        _mock_build(409, unavailable)
        data = _tool_json(await _build(_server))
        assert data == {**unavailable, "http_status": 409}
        assert detail_route.call_count == 1

    @respx.mock
    async def test_build_scenario_non_json_4xx_body_becomes_response_text(self, _server):
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        respx.post(BUILD_URL).mock(
            return_value=httpx.Response(403, text="<html>Forbidden</html>", headers={"content-type": "text/html"})
        )
        data = _tool_json(await _build(_server))
        assert data == {"response": "<html>Forbidden</html>", "http_status": 403}

    @respx.mock
    async def test_build_scenario_5xx_on_build_is_still_a_tool_error(self, _server):
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        respx.post(BUILD_URL).mock(return_value=httpx.Response(502, text="<html>Bad Gateway</html>"))
        text = _tool_error_text(await _build(_server))
        assert "502" in text
        assert "Bad Gateway" not in text  # 5xx bodies are HTML, never relayed

    @pytest.mark.parametrize("in_flight", ["created", "building"])
    @respx.mock
    async def test_build_scenario_resumes_polling_when_latest_run_is_in_flight_without_posting(
        self, _server, in_flight
    ):
        detail_route = respx.get(SCENARIO_URL).mock(
            side_effect=[
                httpx.Response(200, json=_scenario(in_flight, latest_run=_run(501, in_flight))),
                httpx.Response(200, json=_scenario("building", latest_run=_run(501, "building"))),
                httpx.Response(
                    200, json=_scenario("built", latest_run=_run(501, "built"), latest_run_is_valid=True)
                ),
            ]
        )
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called
        assert not boundary_route.called  # the re-call checks run BEFORE the spend gates
        assert detail_route.call_count == 3
        assert data["outcome"] == "built"
        assert data["posted"] is False
        assert data["run_id"] == 501

    @respx.mock
    async def test_build_scenario_resumes_polling_on_the_409_dedup_body_without_a_second_post(
        self, _server
    ):
        """The pre-check saw no run but a build was dispatched before the POST landed:
        the dedup body has NO error_code — poll THAT run, never POST again."""
        detail_route = respx.get(SCENARIO_URL).mock(
            side_effect=[
                httpx.Response(200, json=_scenario()),
                httpx.Response(200, json=_scenario("building", latest_run=_run(501, "building"))),
                httpx.Response(
                    200, json=_scenario("built", latest_run=_run(501, "built"), latest_run_is_valid=True)
                ),
            ]
        )
        _mock_boundary(True)
        build_route = _mock_build(409, DEDUP_409)
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert detail_route.call_count == 3
        assert data["outcome"] == "built"
        assert data["http_status"] == 409
        assert data["build"] == DEDUP_409
        assert data["run_id"] == 501

    @pytest.mark.parametrize("done", ["built", "complete"])
    @respx.mock
    async def test_build_scenario_built_or_complete_without_rebuild_returns_the_state_and_never_posts(
        self, _server, done
    ):
        """`complete` is NOT dedup-blocked server-side (BUILD_DEDUP_BLOCKING_STATUS_VALUES =
        in-flight + created): a blind re-POST would dispatch a duplicate build."""
        detail_route = respx.get(SCENARIO_URL).mock(
            return_value=httpx.Response(
                200,
                json=_scenario(
                    done, latest_run=_run(501, done, mesh_triangle_count=19_812), latest_run_is_valid=True
                ),
            )
        )
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called and not boundary_route.called
        assert detail_route.call_count == 1
        assert data["outcome"] == done
        assert data["computed_status"] == done
        assert data["posted"] is False
        assert data["run_id"] == 501
        assert data["mesh_triangle_count"] == 19_812
        assert "rebuild=true" in data["note"]

    @respx.mock
    async def test_build_scenario_built_with_rebuild_true_posts_a_new_build(self, _server):
        _detail_to_built(_scenario("built", latest_run=_run(500, "built"), latest_run_is_valid=True))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server, rebuild=True))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"
        assert data["run_id"] == 501

    @respx.mock
    async def test_build_scenario_built_but_latest_run_is_valid_false_posts_a_rebuild(self, _server):
        """Every PATCH sets latest_run_is_valid False (api_v2.py:3095-3101): the built
        package is stale against the current inputs — rebuild without rebuild=true."""
        _detail_to_built(_scenario("built", latest_run=_run(500, "built"), latest_run_is_valid=False))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"
        assert data["run_id"] == 501

    @pytest.mark.parametrize("dead", ["error", "cancelled"])
    @respx.mock
    async def test_build_scenario_after_an_error_or_cancelled_run_posts_a_rebuild(self, _server, dead):
        _detail_to_built(_scenario(dead, latest_run=_run(500, dead, error_message="boom")))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"
        assert data["run_id"] == 501

    @respx.mock
    async def test_build_scenario_refuses_a_stranger_read_without_posting(self, _server):
        """A public project's scenario read by a non-member is 200 with ONLY
        STRANGER_SCENARIO_FIELDS (serializers_v2.py:395-405): no boundary, no
        computed_status. The build needs EDITOR — say so instead of guessing."""
        stranger = {
            "id": 5, "name": "mcp-w1-3172-t", "description": "", "resolution": 36.0, "duration": 43200,
            "mesh_triangle_count_estimate": 19_729, "mesh_triangle_count_estimate_breakdown": ESTIMATE_BREAKDOWN,
            "latest_run": None, "latest_complete_run": None,
        }
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=stranger))
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called and not boundary_route.called
        assert data["outcome"] == "refused"
        assert "not a project member" in data["reason"] and "EDITOR" in data["reason"]

    @respx.mock
    async def test_build_scenario_timeout_is_bounded_and_names_the_run_id(self, _server):
        """timeout_seconds=0 → the POST, then exactly ONE poll, then `timed_out` naming
        the run so the agent can cancel_run one stuck in `created`; a re-call resumes."""
        detail_route = respx.get(SCENARIO_URL).mock(
            side_effect=[
                httpx.Response(200, json=_scenario()),
                httpx.Response(200, json=_scenario("created", latest_run=_run(501, "created"))),
                httpx.Response(500),
            ]
        )
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server, timeout_seconds=0))
        assert build_route.call_count == 1
        assert detail_route.call_count == 2
        assert data["outcome"] == "timed_out"
        assert data["computed_status"] == "created"
        assert data["run_id"] == 501
        assert data["polls"] == 1
        assert "cancel_run" in data["note"]

    @respx.mock
    async def test_build_scenario_error_end_state_carries_the_run_error_message(self, _server):
        respx.get(SCENARIO_URL).mock(
            side_effect=[
                httpx.Response(200, json=_scenario()),
                httpx.Response(
                    200,
                    json=_scenario(
                        "error", latest_run=_run(501, "error", error_message="Could not find Boundary data")
                    ),
                ),
            ]
        )
        _mock_boundary(True)
        _mock_build()
        data = _tool_json(await _build(_server))
        assert data["outcome"] == "error"
        assert data["run_id"] == 501
        assert data["error_message"] == "Could not find Boundary data"

    async def test_build_scenario_and_create_scenario_listed_with_computed_status_metres_and_confirm(
        self, _server
    ):
        """The docstring ACs, made provable: the units trap (a LENGTH in metres on the
        scenario; an AREA on a MeshRegion feature), the poll key and the confirm gate
        are on the tools' OWN descriptions; get_scenario's names computed_status."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        build = tools["build_scenario"]["description"]
        for token in ("computed_status", "resolution", "metres", "confirm", "100,000", "MESH_TOO_LARGE"):
            assert token in build, token
        create = tools["create_scenario"]["description"]
        for token in ("resolution", "metres", "m²", "resolution²/2", "computed_status", "MeshRegion"):
            assert token in create, token
        get_scenario = tools["get_scenario"]["description"]
        assert "computed_status" in get_scenario
        props = tools["build_scenario"]["inputSchema"]["properties"]
        assert "confirm" in props and "rebuild" in props and "timeout_seconds" in props
        assert len(tools) == 17  # the module docstring's count


# ---------------------------------------------------------------------------
# TASK-3187 (W0, epic 3200) — presigned-URL elision + the compact get_scenario.
#
# A remote MCP with credential pass-through must not widen what a transcript
# can leak beyond what the caller asked for. A live scenario detail carries TEN
# presigned S3 URLs (five `s3_*_url` keys on `latest_run` and five more on
# `latest_complete_run`), each a 6-hour capability with an STS token; the read
# tools relayed them verbatim into the agent transcript. One recursive helper
# elides them for every read tool + start_simulation; `presign_terrain_upload`
# is the SOLE documented exception (the caller asked for that URL).
#
# The stored proof selects on `-k "presigned or elide or get_scenario"`, so
# every test NAME below carries one of those LITERAL tokens — `pytest -k` is
# case-sensitive and does not match a class name, so `TestGetScenario` is not
# a selection (that is why the proof was RED at HEAD: `116 deselected`).
# ---------------------------------------------------------------------------
# A fake SigV4 query with all six X-Amz-* params a real presigned URL carries:
# redacting only the matched param would leave X-Amz-Credential behind.
SIGNED_QUERY = (
    "X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=FAKEKEYIDNOTREAL%2F20260922%2Fus-west-2%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260922T000000Z&X-Amz-Expires=21600&X-Amz-SignedHeaders=host"
    "&X-Amz-Security-Token=fake-sts-token-for-tests&X-Amz-Signature=deadbeefdeadbeef"
)
# What an elided value (or an elided substring of a log line) reads as.
ELIDED = "[presigned-url-elided]"
# The five signed keys on a run record (serializers_v2.py RunSerializerV2).
RUN_S3_URL_KEYS = (
    "s3_package_url",
    "s3_result_package_url",
    "s3_depth_max_url",
    "s3_velocity_max_url",
    "s3_depth_integrated_velocity_max_url",
)
# A run's WMS dict, shaped like the live one on run 67181: its NAME matches
# neither `*_url` nor `s3_*`, but it NESTS a `url` (GeoServer OWS) and a
# `catalogURL` (catalogue CSW) that a case-insensitive "url in key" rule eats.
GN_LAYER_DEPTH_MAX = {
    "pk": 1538,
    "name": "dep_67181_depth_max",
    "title": "Depth max",
    "alternate": "geonode:dep_67181_depth_max",
    "url": "http://localhost:8080/geoserver/ows",
    "catalogURL": (
        "http://localhost:8081/catalogue/csw?outputschema="
        "http%3A%2F%2Fwww.isotc211.org%2F2005%2Fgmd&service=CSW&request=GetRecordById"
        "&version=2.0.2&elementsetname=full&id=dep-67181-depth-max"
    ),
    "visibility": True,
}
# Unsigned, relative — the only genuine top-level survivor on a terrain record.
BBOX_STATS_URL = "/api/v2/anuga/projects/16132/terrain/110535/bbox-stats/"


def _signed_url(key):
    return f"https://anuga-test-storage.s3.amazonaws.com/{key}?{SIGNED_QUERY}"


def _presigned_run(pk=67181, status="complete", **extra):
    """A run record as RunSerializerV2 serialises it: five signed `s3_*_url`
    keys, the WMS dicts, and a build log."""
    run = _run(pk, status, **extra)
    run.update({key: _signed_url(f"runs/{pk}/{key}") for key in RUN_S3_URL_KEYS})
    run["gn_layer_depth_max"] = dict(GN_LAYER_DEPTH_MAX)
    return run


class TestElidePresignedUrls:
    """One recursive helper, applied per tool — never inside `_post_result`."""

    async def test_elide_presigned_walks_dicts_lists_and_leaves_plain_values_alone(self, _server):
        body = {
            "count": 1,
            "results": [
                {
                    "id": 67181,
                    "s3_package_url": _signed_url("runs/67181/package.zip"),
                    "nested": {"deep": [{"handoff": _signed_url("x")}]},
                    "gn_layer_depth_max": dict(GN_LAYER_DEPTH_MAX),
                    "bbox_stats_url": BBOX_STATS_URL,
                    "mesh_triangle_count": 256_869,
                    "error_message": None,
                    "flag": True,
                }
            ],
        }
        out = _server._elide_presigned(body)
        row = out["results"][0]
        assert out["count"] == 1
        assert row["s3_package_url"] == ELIDED
        assert row["nested"]["deep"][0]["handoff"] == ELIDED
        # Untouched: not a capability.
        assert row["gn_layer_depth_max"] == GN_LAYER_DEPTH_MAX
        assert row["bbox_stats_url"] == BBOX_STATS_URL
        assert row["mesh_triangle_count"] == 256_869
        assert row["error_message"] is None
        assert row["flag"] is True
        # The input is not mutated — the helper returns a new structure.
        assert "X-Amz-Signature" in body["results"][0]["s3_package_url"]

    async def test_elide_presigned_redacts_a_signed_url_in_place_inside_a_log_line(self, _server):
        log = (
            "2026-09-21 09:00:01 INFO downloading package from "
            + _signed_url("runs/67181/package.zip")
            + " to /tmp/anuga\n2026-09-21 09:04:12 INFO mesh has 256869 triangles"
        )
        out = _server._elide_presigned({"log": log})["log"]
        assert "X-Amz" not in out
        # Redacted IN PLACE: only the URL token went, the rest of the log survives.
        assert out.startswith("2026-09-21 09:00:01 INFO downloading package from " + ELIDED)
        assert "256869 triangles" in out
        assert "/tmp/anuga" in out

    async def test_elide_presigned_leaves_an_unsigned_s3_style_string_alone(self, _server):
        """Only a SIGNED string is a capability; an unsigned URL is just a link."""
        plain = "https://anuga-test-storage.s3.amazonaws.com/runs/67181/package.zip"
        assert _server._elide_presigned({"note": plain})["note"] == plain


class TestGetScenarioCompactState:
    """AC1: the compact state, and never a presigned URL."""

    @staticmethod
    def _detail():
        return _scenario(
            "complete",
            latest_run=_presigned_run(67181, "complete", mesh_triangle_count=256_869),
            latest_complete_run=_presigned_run(67181, "complete", mesh_triangle_count=256_869),
            latest_run_is_valid=True,
            resolution=35.0,
            terrain=110535,
            boundary=27499,
            compute_cost_estimate=0.52,
            vcpu_hours_estimate=3.071667015,
            inflow_anchor_mismatch=None,
        )

    @respx.mock
    async def test_get_scenario_elides_the_ten_presigned_urls_of_both_run_blocks(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(
            return_value=httpx.Response(200, json=self._detail())
        )
        result = await _server.get_scenario(project_id=42, scenario_id=5)
        assert "X-Amz" not in result
        for key in RUN_S3_URL_KEYS:
            assert key not in result, key
        data = json.loads(result)
        # The compact state replaces the detail wholesale: no run blocks, no log.
        assert "latest_run" not in data and "latest_complete_run" not in data
        assert "log" not in result and "perms" not in data

    @respx.mock
    async def test_get_scenario_carries_the_state_the_inputs_and_the_price(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(
            return_value=httpx.Response(200, json=self._detail())
        )
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        # _build_state's ten flat keys (UNFORKED).
        assert data["scenario_id"] == 5
        assert data["computed_status"] == "complete"
        assert data["latest_run_is_valid"] is True
        assert data["run_id"] == 67181
        assert data["run_status"] == "complete"
        assert data["mesh_triangle_count"] == 256_869
        assert data["error_message"] is None
        assert data["user_message"] is None
        assert data["status_detail"] is None
        assert data["mesh_triangle_count_estimate"] == 19_729
        # …plus the estimate breakdown, the inputs and the price: without them a
        # session that did not create the scenario would be blind to what it is
        # about to build and what it will cost (there is no list_scenarios tool).
        assert data["mesh_triangle_count_estimate_breakdown"]["total"] == 19_729
        assert data["name"] == "mcp-w1-3172-t"
        assert data["project"] == 42
        assert data["resolution"] == 35.0
        assert data["duration"] == 43200
        assert data["terrain"] == 110535
        assert data["boundary"] == 27499
        assert data["friction"] == 124557
        assert data["inflow"] == 124555
        assert data["rainfall"] == 124556
        assert data["structure"] == 124558
        assert data["mesh_region"] is None
        assert data["description"] == ""
        assert data["compute_cost_estimate"] == 0.52
        assert data["vcpu_hours_estimate"] == 3.071667015
        # The scenario's only pre-build correctness warning: present even when None.
        assert "inflow_anchor_mismatch" in data

    @respx.mock
    async def test_get_scenario_redacts_a_presigned_url_quoted_in_the_description(self, _server):
        """The compact shape alone would hide a leak: the whitelist copies the
        scenario's own text through, so get_scenario must ALSO go through the
        elision helper (the description is free text a user can paste into)."""
        detail = _scenario(
            "built",
            description="rebuilt from " + _signed_url("runs/67179/package.zip") + " on 2026-09-21",
        )
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(
            return_value=httpx.Response(200, json=detail)
        )
        result = await _server.get_scenario(project_id=42, scenario_id=5)
        assert "X-Amz" not in result
        assert json.loads(result)["description"] == f"rebuilt from {ELIDED} on 2026-09-21"

    @respx.mock
    async def test_get_scenario_does_not_fabricate_nulls_for_a_non_member_record(self, _server):
        """STRANGER_SCENARIO_FIELDS (hydrata serializers_v2.py:381-400) DROPS the
        FKs, the estimates and the perms from a non-member's read. `.get(key)`
        would turn "withheld" into "terrain: null, compute_cost_estimate: null" —
        i.e. "no terrain, no cost". Absent stays absent."""
        stranger = {"id": 5, "name": "mcp-w1-3172-t", "project": 42, "description": ""}
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(
            return_value=httpx.Response(200, json=stranger)
        )
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        assert data["scenario_id"] == 5 and data["name"] == "mcp-w1-3172-t"
        for withheld in ("terrain", "boundary", "compute_cost_estimate", "vcpu_hours_estimate",
                         "resolution", "duration", "inflow_anchor_mismatch"):
            assert withheld not in data, withheld

    @respx.mock
    async def test_get_scenario_tolerates_a_non_dict_detail(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(
            return_value=httpx.Response(200, json="gone")
        )
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        assert data["scenario_id"] is None and data["computed_status"] is None


class TestElidePresignedPerTool:
    """AC2: every read tool + start_simulation, asserted on the TOOL's output."""

    @respx.mock
    async def test_get_run_elides_the_five_presigned_urls_and_keeps_the_log(self, _server):
        run = _presigned_run(
            67181,
            "complete",
            log=(
                "2026-09-21 09:00:01 INFO uploading results to "
                + _signed_url("runs/67181/results.zip")
                + "\n2026-09-21 09:31:44 INFO run complete"
            ),
        )
        respx.get(f"{BASE}/runs/67181/").mock(return_value=httpx.Response(200, json=run))
        result = await _server.get_run(run_id=67181)
        assert "X-Amz" not in result
        data = json.loads(result)
        for key in RUN_S3_URL_KEYS:
            assert data[key] == ELIDED, key
        # AC6: the log is redacted in place, not dropped.
        assert "run complete" in data["log"] and ELIDED in data["log"]

    @respx.mock
    async def test_get_run_elide_keeps_the_gn_layer_dict_byte_equal(self, _server):
        """AC3 (over-elision guard): the nested `url`/`catalogURL` of a WMS dict
        are how results stay VIEWABLE after this card — assert by VALUE, because
        a "key is present" assertion on a dict cannot fail."""
        respx.get(f"{BASE}/runs/67181/").mock(
            return_value=httpx.Response(200, json=_presigned_run())
        )
        data = json.loads(await _server.get_run(run_id=67181))
        assert data["gn_layer_depth_max"] == GN_LAYER_DEPTH_MAX

    @respx.mock
    async def test_list_runs_elides_presigned_urls_nested_in_every_results_row(self, _server):
        """list_runs is the biggest leak by volume: five signed keys per row,
        default page_size 100 — and the body is a {count, results} DICT, so the
        helper has to RECURSE into the list to reach them."""
        respx.get(f"{BASE}/projects/42/runs/").mock(
            return_value=httpx.Response(
                200, json={"count": 2, "results": [_presigned_run(67180), _presigned_run(67181)]}
            )
        )
        result = await _server.list_runs(project_id=42)
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["count"] == 2
        assert [row["id"] for row in data["results"]] == [67180, 67181]
        assert all(row["s3_package_url"] == ELIDED for row in data["results"])

    @respx.mock
    async def test_start_simulation_elides_the_presigned_package_url_of_the_built_run(self, _server):
        """A `built` run ALREADY holds a signed package URL, and that is exactly
        the state start_simulation is called in — so every dispatch relayed a
        6-hour capability back into the transcript."""
        respx.post(f"{BASE}/scenarios/5/run/").mock(
            return_value=httpx.Response(202, json=_presigned_run(67182, "queued"))
        )
        result = await _server.start_simulation(scenario_id=5)
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["http_status"] == 202
        assert data["status"] == "queued"
        assert data["s3_package_url"] == ELIDED

    @respx.mock
    async def test_get_run_status_elides_a_presigned_url_under_an_unknown_key(self, _server):
        """get_run_status carries no signed key TODAY — the value-shape rule is
        what makes a key added server-side tomorrow safe by default."""
        respx.get(f"{BASE}/runs/67181/status/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 67181,
                    "status": "computing",
                    "progress_pct": 45,
                    "artifact_handoff": _signed_url("runs/67181/partial.zip"),
                },
            )
        )
        result = await _server.get_run_status(run_id=67181)
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["progress_pct"] == 45
        assert data["artifact_handoff"] == ELIDED

    @respx.mock
    async def test_get_project_elides_a_presigned_url_nested_under_an_unknown_key(self, _server):
        respx.get(f"{BASE}/projects/42/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 42,
                    "name": "Towradgi",
                    "base_map": 1525,
                    "exports": [{"handoff": _signed_url("exports/42.zip")}],
                },
            )
        )
        result = await _server.get_project(project_id=42)
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["name"] == "Towradgi" and data["base_map"] == 1525
        assert data["exports"][0]["handoff"] == ELIDED

    @respx.mock
    async def test_list_projects_elides_a_presigned_url_in_a_project_row(self, _server):
        respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "count": 1,
                    "results": [{"id": 42, "name": "Towradgi",
                                 "handoff": _signed_url("exports/42.zip")}],
                },
            )
        )
        result = await _server.list_projects()
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["results"][0]["name"] == "Towradgi"
        assert data["results"][0]["handoff"] == ELIDED

    @respx.mock
    async def test_get_terrain_elides_a_presigned_url_in_the_composed_result(self, _server):
        """get_terrain does NOT relay the body verbatim — it composes
        {outcome, status, polls, elapsed_seconds, terrain}: elide the COMPOSED
        result. AC3's other half rides here: `bbox_stats_url` is unsigned and
        relative and must survive EXACTLY."""
        terrain = _terrain(
            110535,
            "ready",
            bbox_stats_url=BBOX_STATS_URL,
            download_handoff=_signed_url("terrain/110535.tif"),
        )
        respx.get(f"{BASE}/projects/42/terrain/110535/").mock(
            return_value=httpx.Response(200, json=terrain)
        )
        result = await _server.get_terrain(
            project_id=42, terrain_id=110535, timeout_seconds=0, poll_interval_seconds=0
        )
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["outcome"] == "ready" and data["status"] == "ready"
        assert data["terrain"]["bbox_stats_url"] == BBOX_STATS_URL
        assert data["terrain"]["download_handoff"] == ELIDED

    @respx.mock
    async def test_presign_terrain_upload_still_returns_its_presigned_url_intact(self, _server):
        """PRE-DECIDED #4 — the SOLE documented exception: the caller asked for
        this URL, so the helper must never be wired into `_post_result` (which
        presign/finalize/create_project/create_time_series/attach_input_layer/
        cancel_run/retry_run all share)."""
        upload_url = _signed_url("terrain_uploads/staging/dem.tif")
        respx.post(f"{BASE}/projects/42/terrain/upload/presign/").mock(
            return_value=httpx.Response(
                201,
                json={
                    "upload_url": upload_url,
                    "staging_key": STAGING_KEY,
                    "process_id": PROCESS_ID,
                    "method": "PUT",
                    "expires_in": 3600,
                },
            )
        )
        result = await _server.presign_terrain_upload(project_id=42, filename=DEM_NAME)
        assert json.loads(result)["upload_url"] == upload_url
        assert "X-Amz-Signature" in result
