"""TASK-3641 (epic 3585 W2b) -> TASK-4053 (epic 4051 W1.2): the culvert kind.

attach_input_layer kind=culvert is the same one path as every other kind since
TASK-4053: the uploaded dataset is PATCHed onto the culvert row and Hydrata's own
canonical conversion does the rest. The MCP's read-back / attribute mapping /
re-upload / default-row creation (and their tests) are gone; tests/
test_attach_one_path.py pins that. create_scenario still takes `culvert`.
"""

import importlib
import json

import httpx
import pytest
import respx

from tests.test_server import (
    BASE,
    CALLER_KEY,
    EXEC_ID,
    EXEC_STATUS_URL,
    ORIGIN,
    _call_as_caller,
    _dataset,
    _exec,
    _row,
    _tool_json,
)

pytestmark = pytest.mark.asyncio

CULVERTS = f"{BASE}/projects/42/culverts/"


@pytest.fixture
def _server(env_vars):
    """The server module, reloaded after the env vars are set (as in test_server)."""
    import hydrata_mcp.server as srv
    importlib.reload(srv)
    return srv


async def _attach(srv, **extra):
    args = {"project_id": 42, "kind": "culvert", "execution_id": EXEC_ID, "poll_interval_seconds": 0}
    args.update(extra)
    return _tool_json(await _call_as_caller(srv, "attach_input_layer", args))


class TestCulvertKindAttach:
    @respx.mock
    async def test_culvert_kind_attach_patches_the_uploaded_dataset_straight_onto_the_row(self, _server):
        status = respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:cul_42_culverts")))
        respx.get(CULVERTS).mock(return_value=httpx.Response(
            200, json=[_row(9, "Culvert 01", gn_layer=1450)]))
        patch = respx.patch(f"{CULVERTS}9/").mock(return_value=httpx.Response(
            200, json=_row(9, "Culvert 01", gn_layer=1700)))
        respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1525}))
        respx.get(url__startswith=f"{ORIGIN}/api/v2/").mock(return_value=httpx.Response(200, json={"datasets": []}))
        # Any other request (a features read-back, an upload, a POST) would be unmocked and fail.
        data = await _attach(_server)
        # TASK-3918: dataset_pk is the converted layer the row now uses; the upload is source_dataset_pk.
        assert data["outcome"] == "attached" and data["row_id"] == 9 and data["dataset_pk"] == 1700
        assert data["source_dataset_pk"] == 1502
        assert json.loads(patch.calls[0].request.content) == {"gn_layer": 1502}
        assert patch.calls[0].request.headers["authorization"] == CALLER_KEY
        assert status.called
        assert "mapped" not in data and "leftover_datasets" not in data and "culvert_problems" not in data

    @respx.mock
    async def test_culvert_kind_without_a_row_says_to_use_write_input_layer(self, _server):
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        respx.get(CULVERTS).mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1525}))
        posts = respx.post(CULVERTS).mock(return_value=httpx.Response(201, json={"id": 9}))
        data = await _attach(_server)
        assert data["outcome"] == "no_default_row"
        assert "write_input_layer" in data["message"]
        assert not posts.called


class TestCulvertKindScenario:
    @respx.mock
    async def test_culvert_kind_create_scenario_sends_culvert_and_reads_it_back(self, _server):
        post = respx.post(f"{BASE}/projects/42/scenarios/").mock(
            return_value=httpx.Response(201, json={"id": 77}))
        respx.get(f"{BASE}/projects/42/scenarios/77/").mock(
            return_value=httpx.Response(200, json={"id": 77, "name": "s", "culvert": 9}))
        respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1}))
        data = json.loads(await _server.create_scenario(
            project_id=42, name="s", resolution=10, duration=60, culvert=9))
        assert json.loads(post.calls[0].request.content)["culvert"] == 9
        assert data["culvert"] == 9

    @respx.mock
    async def test_culvert_kind_create_scenario_omits_culvert_when_not_given(self, _server):
        post = respx.post(f"{BASE}/projects/42/scenarios/").mock(
            return_value=httpx.Response(201, json={"id": 77}))
        respx.get(f"{BASE}/projects/42/scenarios/77/").mock(
            return_value=httpx.Response(200, json={"id": 77, "name": "s"}))
        respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1}))
        await _server.create_scenario(project_id=42, name="s", resolution=10, duration=60)
        assert "culvert" not in json.loads(post.calls[0].request.content)
