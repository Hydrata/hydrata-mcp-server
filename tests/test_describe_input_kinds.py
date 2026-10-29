"""TASK-4057 (epic 4051 W1.6) — describe_input_kinds wraps Hydrata's generated input-kinds endpoint."""

import importlib

import httpx
import pytest
import respx

from tests.test_server import BASE, CALLER_KEY, _call_as_caller, _tool_error_text, _tool_json

pytestmark = pytest.mark.asyncio

URL = f"{BASE}/input-kinds/"
KINDS = ["boundary", "friction", "inflow", "rainfall", "building", "mesh_region", "culvert"]
PAYLOAD = {
    "note": "Send a GeoJSON FeatureCollection.",
    "kinds": {
        k: {
            "geometry": "LineString",
            "properties": {"x": {"type": "string", "required": False}},
            "required_one_of": [],
            "notes": [],
            "example": {"type": "FeatureCollection", "features": []},
        }
        for k in KINDS
    },
}


@pytest.fixture
def _server(env_vars):
    import hydrata_mcp.server as srv

    importlib.reload(srv)
    return srv


@respx.mock
async def test_describe_input_kinds_returns_all_seven_kinds_and_forwards_the_key(_server):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
    data = _tool_json(await _call_as_caller(_server, "describe_input_kinds", {}))
    assert route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert list(data["kinds"]) == KINDS and data["note"]


@respx.mock
async def test_describe_input_kinds_one_kind_narrows_the_answer(_server):
    respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
    data = _tool_json(await _call_as_caller(_server, "describe_input_kinds", {"kind": " Mesh-Region "}))
    assert list(data["kinds"]) == ["mesh_region"]


@respx.mock
async def test_describe_input_kinds_an_unknown_kind_is_refused_naming_the_choices(_server):
    respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
    text = _tool_error_text(await _call_as_caller(_server, "describe_input_kinds", {"kind": "breakline"}))
    assert "boundary" in text and "culvert" in text


@respx.mock
async def test_describe_input_kinds_an_unexpected_body_is_an_error_not_an_empty_answer(_server):
    respx.get(URL).mock(return_value=httpx.Response(200, json={"nope": 1}))
    assert "did not return" in _tool_error_text(await _call_as_caller(_server, "describe_input_kinds", {}))
