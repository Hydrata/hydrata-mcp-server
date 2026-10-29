"""TASK-4058 (epic 4051 W2.1) — request_input_upload: the browser link for an input too big to send inline.

The tool moves no bytes. It returns the project's upload-page link for one kind; the
person drops the file there and Hydrata converts and attaches it (authored_method
'uploaded by the user'), which list_inputs then shows.
"""

import importlib

import httpx
import pytest
import respx

from tests.test_server import BASE, _call_as_caller, _tool_error_text, _tool_json

pytestmark = pytest.mark.asyncio

PROJECT = f"{BASE}/projects/42/"


@pytest.fixture
def _server(env_vars):
    import hydrata_mcp.server as srv
    importlib.reload(srv)
    return srv


@respx.mock
async def test_request_input_upload_returns_the_upload_page_for_the_project_and_kind(_server):
    respx.get(PROJECT).mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 3}))
    data = _tool_json(await _call_as_caller(_server, "request_input_upload", {"project_id": 42, "kind": "building"}))
    assert data["ui_url"].endswith("/catalogue/#/map/3?panel=input-upload&kind=building")
    assert data["kind"] == "building" and data["limit_mb"] == 100
    assert "shapefile" in data["say_to_user"].lower() and "list_inputs" in data["say_to_user"]


@respx.mock
async def test_request_input_upload_maps_the_legacy_structure_kind_and_refuses_unknown_kinds(_server):
    route = respx.get(PROJECT).mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 3}))
    data = _tool_json(await _call_as_caller(_server, "request_input_upload", {"project_id": 42, "kind": "Structure"}))
    assert data["kind"] == "building" and "deprecation" in data
    calls = route.call_count
    for kind in ("breakline", "terrain", ""):
        _tool_error_text(await _call_as_caller(_server, "request_input_upload", {"project_id": 42, "kind": kind}))
    assert route.call_count == calls, "a bad kind is refused before any request"


@respx.mock
async def test_request_input_upload_without_a_base_map_says_so_instead_of_a_dead_link(_server):
    respx.get(PROJECT).mock(return_value=httpx.Response(200, json={"id": 42, "base_map": None}))
    text = _tool_error_text(await _call_as_caller(_server, "request_input_upload", {"project_id": 42, "kind": "friction"}))
    assert "map" in text.lower()


@respx.mock
async def test_the_over_cap_refusal_carries_the_exact_call_that_gets_the_link(_server):
    line = {"type": "LineString", "coordinates": [[151.0, -33.8], [151.01, -33.8]]}
    big = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": line, "properties": {"note": "x" * 200}} for _ in range(800)]}
    text = _tool_error_text(await _call_as_caller(
        _server, "write_input_layer", {"project_id": 42, "kind": "inflow", "features": big}))
    assert "request_input_upload(project_id=42, kind='inflow')" in text
