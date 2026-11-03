"""TASK-4107 (epic 4117 W1) — read_input_layer: the agent reads a layer's features (fids, attributes, author)
before it writes, through Django (agents are refused on /gs/)."""

import httpx
import pytest
import respx

from tests.helpers import BASE, CALLER_KEY, _call_as_caller, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

DEFAULT = f"{BASE}/projects/42/input-layers/culvert/features/"
ROW = f"{BASE}/projects/42/input-layers/culvert/321/features/"
BODY = {
    "type": "FeatureCollection",
    "kind": "culvert",
    "row_id": 321,
    "features": [
        {"type": "Feature", "id": 1, "properties": {"name": "test1"}, "author": {"type": "person"}},
        {"type": "Feature", "id": 2, "properties": {"name": "Mouat"}, "author": {"type": "agent", "grant": "Claude"}},
    ],
}


@pytest.fixture
def _server(env_vars):
    return reload_server()


@respx.mock
async def test_read_input_layer_reads_the_default_row_and_counts_the_persons_features(_server):
    route = respx.get(DEFAULT).mock(return_value=httpx.Response(200, json=BODY))
    data = _tool_json(await _call_as_caller(_server, "read_input_layer", {"project_id": 42, "kind": "culvert"}))
    assert route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert data["feature_count"] == 2 and data["person_features"] == 1
    assert [f["id"] for f in data["features"]] == [1, 2]


@respx.mock
async def test_read_input_layer_reads_a_named_row(_server):
    route = respx.get(ROW).mock(return_value=httpx.Response(200, json=BODY))
    await _call_as_caller(_server, "read_input_layer", {"project_id": 42, "kind": "culvert", "row_id": 321})
    assert route.call_count == 1


@respx.mock
async def test_read_input_layer_refuses_an_unknown_kind_before_any_request(_server):
    text = _tool_error_text(await _call_as_caller(_server, "read_input_layer", {"project_id": 42, "kind": "breakline"}))
    assert "not one of" in text


@respx.mock
async def test_read_input_layer_is_read_only(_server):
    tools = {t.name: t for t in await _server.mcp.list_tools()}
    assert tools["read_input_layer"].annotations.read_only_hint is True


async def test_instructions_say_read_first_add_and_ask_before_replace(_server):
    text = _server.mcp.instructions.lower()
    for phrase in ("read the layer first", "add to what the person drew", "ask before any replace"):
        assert phrase in text
