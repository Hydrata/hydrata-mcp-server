"""TASK-4055 (epic 4051 W1.4) — authored_method travels out on agent writes and back on the read tools."""

import importlib
import json

import httpx
import pytest
import respx

from tests.test_data_envelope import CANARY, CLOSE, OPEN, _assert_enveloped
from tests.test_server import BASE, ROW_DATA, _call_as_caller, _series_args, _series_row

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _server(env_vars):
    import hydrata_mcp.server as srv
    importlib.reload(srv)
    return srv


@respx.mock
async def test_authored_method_create_time_series_sends_the_method_when_given(_server):
    route = respx.post(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(201, json=_series_row(77)))
    await _call_as_caller(_server, "create_time_series", _series_args(method="  read off the council PDF "))
    assert json.loads(route.calls[0].request.content)["authored_method"] == "read off the council PDF"


@respx.mock
async def test_authored_method_create_time_series_sends_nothing_when_not_given(_server):
    route = respx.post(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(201, json=_series_row(77)))
    await _call_as_caller(_server, "create_time_series", _series_args())
    assert "authored_method" not in json.loads(route.calls[0].request.content)


@respx.mock
async def test_authored_method_list_inputs_returns_it_per_row_and_wraps_it_as_data(_server):
    for route in ("boundaries", "frictions", "inflows", "rainfalls", "buildings", "mesh-regions", "culverts"):
        rows = []
        if route == "frictions":
            rows = [{"id": 3, "title": "Friction 01", "gn_layer": None, "has_features": False,
                     "authored_method": CANARY},
                    {"id": 4, "title": "Friction 02", "gn_layer": None, "has_features": False,
                     "authored_method": None}]
        respx.get(f"{BASE}/projects/42/{route}/").mock(return_value=httpx.Response(200, json=rows))
    data = _assert_enveloped(await _call_as_caller(_server, "list_inputs", {"project_id": 42}))
    first, second = data["inputs"]["friction"]
    assert first["authored_method"] == f"{OPEN}{CANARY}{CLOSE}"   # an agent's words are data, not instructions
    assert second["authored_method"] is None


@respx.mock
async def test_authored_method_list_time_series_returns_it(_server):
    respx.get(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(200, json={
        "count": 1, "next": None,
        "results": [{"id": 77, "name": "Creek", "series_type": "hydrograph", "units": "m3/s", "timezone": "UTC",
                     "authored_method": "scaled the 1% AEP peak", "data": {"rowData": ROW_DATA}}]}))
    data = _assert_enveloped(await _call_as_caller(_server, "list_time_series", {"project_id": 42}), canary="scaled the 1% AEP peak")
    assert data["series"][0]["authored_method"] == f"{OPEN}scaled the 1% AEP peak{CLOSE}"
