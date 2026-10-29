"""TASK-4052 (epic 4051 W1.1) — write_input_layer: the features travel in the call.

One POST to the sync endpoint, no importer, no execution_id; the key is forwarded;
a payload over ~150 KB is refused up front, naming the browser upload link; a
Hydrata refusal reaches the agent as a refusal.
"""

import importlib
import json

import httpx
import pytest
import respx

from tests.test_server import BASE, CALLER_KEY, ORIGIN, _call_as_caller, _tool_error_text, _tool_json

pytestmark = pytest.mark.asyncio

WRITE = f"{BASE}/projects/42/input-layers/write/"
LINE = {"type": "LineString", "coordinates": [[151.0, -33.8], [151.01, -33.8]]}
FC = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": LINE, "properties": {"name": "C1"}}]}


@pytest.fixture
def _server(env_vars):
    import hydrata_mcp.server as srv

    importlib.reload(srv)
    return srv


def _written(kind="culvert"):
    return {
        "kind": kind,
        "row_id": 9,
        "row_title": "Culvert 01",
        "created_row": True,
        "gn_layer": 77,
        "dataset_alternate": "geonode:cul_42_culvert_01",
        "feature_count": 1,
    }


@respx.mock
async def test_write_input_layer_posts_the_features_once_and_forwards_the_key(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written()))
    respx.get(f"{ORIGIN}/api/v2/datasets/").mock(return_value=httpx.Response(200, json={"datasets": []}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 3}))
    data = _tool_json(
        await _call_as_caller(
            _server, "write_input_layer", {"project_id": 42, "kind": "culvert", "features": FC, "method": "traced"}
        )
    )
    assert route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert json.loads(route.calls[0].request.content) == {
        "kind": "culvert",
        "features": FC,
        "authored_method": "traced",
    }
    assert data["outcome"] == "written" and data["row_id"] == 9 and data["feature_count"] == 1


@respx.mock
async def test_write_input_layer_refuses_a_payload_over_the_cap_naming_the_upload_link(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written()))
    big = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": LINE, "properties": {"note": "x" * 200}} for _ in range(800)],
    }
    text = _tool_error_text(
        await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": "inflow", "features": big})
    )
    assert "KB" in text and "request_input_upload" in text and "Nothing was sent" in text
    assert not route.called


@respx.mock
async def test_write_input_layer_relays_a_hydrata_refusal(_server):
    respx.post(WRITE).mock(
        return_value=httpx.Response(
            400,
            json={
                "error_code": "VALIDATION_ERROR",
                "detail": "feature #2 is a Point; a inflow layer holds LineString features only",
            },
        )
    )
    text = _tool_error_text(
        await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": "inflow", "features": FC})
    )
    assert "feature #2 is a Point" in text


@respx.mock
async def test_write_input_layer_refuses_breakline_and_unknown_kinds_without_a_request(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written()))
    for kind in ("breakline", "terrain"):
        _tool_error_text(
            await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": kind, "features": FC})
        )
    assert not route.called


@respx.mock
async def test_write_input_layer_maps_the_legacy_structure_kind_to_building(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written("building")))
    respx.get(f"{ORIGIN}/api/v2/datasets/").mock(return_value=httpx.Response(200, json={"datasets": []}))
    data = _tool_json(
        await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": "structure", "features": FC})
    )
    assert json.loads(route.calls[0].request.content)["kind"] == "building"
    assert "deprecation" in data
