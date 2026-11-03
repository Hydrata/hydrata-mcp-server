"""TASK-4055 (epic 4051 W1.4) — authored_method travels out on agent writes and back on the read tools."""

import json

import httpx
import pytest
import respx

from tests.helpers import BASE, ROW_DATA, _call_as_caller, _series_args, _series_row, reload_server
from tests.test_data_envelope import CANARY, CLOSE, OPEN, _assert_enveloped

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _server(env_vars):
    return reload_server()


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
            rows = [
                {"id": 3, "title": "Friction 01", "gn_layer": None, "has_features": False, "authored_method": CANARY},
                {"id": 4, "title": "Friction 02", "gn_layer": None, "has_features": False, "authored_method": None},
            ]
        respx.get(f"{BASE}/projects/42/{route}/").mock(return_value=httpx.Response(200, json=rows))
    data = _assert_enveloped(await _call_as_caller(_server, "list_inputs", {"project_id": 42}))
    first, second = data["inputs"]["friction"]
    assert first["authored_method"] == f"{OPEN}{CANARY}{CLOSE}"  # an agent's words are data, not instructions
    assert second["authored_method"] is None


@respx.mock
async def test_authored_method_list_time_series_returns_it(_server):
    respx.get(f"{BASE}/projects/42/time-series/").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "next": None,
                "results": [
                    {
                        "id": 77,
                        "name": "Creek",
                        "series_type": "hydrograph",
                        "units": "m3/s",
                        "timezone": "UTC",
                        "authored_method": "scaled the 1% AEP peak",
                        "data": {"rowData": ROW_DATA},
                    }
                ],
            },
        )
    )
    data = _assert_enveloped(
        await _call_as_caller(_server, "list_time_series", {"project_id": 42}), canary="scaled the 1% AEP peak"
    )
    assert data["series"][0]["authored_method"] == f"{OPEN}scaled the 1% AEP peak{CLOSE}"


GHD = {"title": "GHD 2015 Sullivans Creek Flood Study", "url": "https://example.org/ghd-2015", "locator": "Table 5-2"}


@respx.mock
async def test_sources_link_create_time_series_sends_the_sources_beside_the_method_and_echoes_them(_server):
    route = respx.post(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(201, json=_series_row(77)))
    result = await _call_as_caller(
        _server, "create_time_series", _series_args(method="GHD Table 5-2", sources=[{**GHD, "locator": " Table 5-2 "}])
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["sources"] == [GHD] and sent["authored_method"] == "GHD Table 5-2"
    assert json.loads(result["content"][0]["text"])["sources"][0]["title"].endswith(GHD["title"] + CLOSE)


@respx.mock
async def test_sources_link_create_time_series_sends_no_sources_key_when_none_given(_server):
    route = respx.post(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(201, json=_series_row(77)))
    await _call_as_caller(_server, "create_time_series", _series_args())
    assert "sources" not in json.loads(route.calls[0].request.content)


@respx.mock
@pytest.mark.parametrize(
    "bad",
    [
        "a string",
        [{"url": "https://x.test"}],
        [{"title": "T"}],
        [{"title": "T", "url": "javascript:alert(1)"}],
        [{"title": "T", "url": "https://x.test", "locator": "L" * 101}],
        [{"title": "T", "url": "https://x.test"}] * 21,
    ],
)
async def test_sources_link_a_malformed_list_is_refused_before_any_request(_server, bad):
    route = respx.post(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(201, json=_series_row(77)))
    result = await _call_as_caller(_server, "create_time_series", _series_args(sources=bad))
    assert result.get("isError") and not route.called


@respx.mock
async def test_sources_link_update_time_series_sends_them_with_the_new_rows(_server):
    route = respx.patch(f"{BASE}/projects/42/time-series/77/").mock(
        return_value=httpx.Response(200, json={**_series_row(77), "sources": [GHD]})
    )
    await _call_as_caller(
        _server,
        "update_time_series",
        {"project_id": 42, "series_id": 77, "data": {"rowData": ROW_DATA}, "sources": [GHD]},
    )
    assert json.loads(route.calls[0].request.content)["sources"] == [GHD]


@respx.mock
async def test_sources_link_write_input_layer_sends_and_echoes_the_sources(_server):
    route = respx.post(url__regex=r".*/projects/42/input-layers/write/").mock(
        return_value=httpx.Response(201, json={"kind": "boundary", "row_id": 5, "feature_count": 1})
    )
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    features = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, "properties": {}}
        ],
    }
    result = await _call_as_caller(
        _server,
        "write_input_layer",
        {"project_id": 42, "kind": "boundary", "features": features, "method": "traced", "sources": [GHD]},
    )
    assert json.loads(route.calls[0].request.content)["sources"] == [GHD]
    assert "ghd-2015" in result["content"][0]["text"]


@respx.mock
async def test_sources_link_create_scenario_sends_them_in_the_body(_server):
    route = respx.post(f"{BASE}/projects/42/scenarios/").mock(return_value=httpx.Response(201, json={"id": 9}))
    respx.get(f"{BASE}/projects/42/scenarios/9/").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "S", "resolution": 40, "duration": 3600})
    )
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    await _call_as_caller(
        _server,
        "create_scenario",
        {"project_id": 42, "name": "S", "resolution": 40, "duration": 3600, "sources": [GHD]},
    )
    assert json.loads(route.calls[0].request.content)["sources"] == [GHD]


@respx.mock
async def test_sources_link_list_inputs_and_list_time_series_return_them(_server):
    for route in ("boundaries", "frictions", "inflows", "rainfalls", "buildings", "mesh-regions", "culverts"):
        rows = (
            [{"id": 3, "title": "Boundary 01", "gn_layer": None, "has_features": False, "sources": [GHD]}]
            if route == "boundaries"
            else []
        )
        respx.get(f"{BASE}/projects/42/{route}/").mock(return_value=httpx.Response(200, json=rows))
    data = json.loads((await _call_as_caller(_server, "list_inputs", {"project_id": 42}))["content"][0]["text"])
    assert GHD["url"] in data["inputs"]["boundary"][0]["sources"][0]["url"]
    respx.get(f"{BASE}/projects/42/time-series/").mock(
        return_value=httpx.Response(
            200, json={"count": 1, "next": None, "results": [{**_series_row(77), "sources": [GHD]}]}
        )
    )
    series = json.loads((await _call_as_caller(_server, "list_time_series", {"project_id": 42}))["content"][0]["text"])
    assert GHD["url"] in json.dumps(series)


def test_sources_link_instruction_sentence_is_pinned(_server):
    text = _server.mcp.instructions
    assert "SOURCES: when an input comes from a document" in text
    assert "sources=[{title, url, locator}] beside authored_method" in text
