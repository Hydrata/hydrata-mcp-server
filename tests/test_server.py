"""Integration tests for Hydrata MCP server tools.

Uses respx to mock httpx requests so no real API calls are made.
Tests each tool via direct invocation of the async tool functions.
"""

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
