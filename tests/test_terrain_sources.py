"""TASK-4059 (epic 4051 W3.1) — list_terrain_sources + create_terrain_from_source.

Both wrap existing Hydrata routes. The listing keeps the server's finest-first order
(GA 5 m before Copernicus 30 m for the Sullivans Creek box); the create call polls with
get_terrain's helper and carries source, resolution, licence and datum; refusals are words.
"""

import json

import httpx
import pytest
import respx

from tests.helpers import BASE, CALLER_KEY, _call_as_caller, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

SULLIVANS = [149.105, -35.285, 149.135, -35.255]
SOURCES = f"{BASE}/projects/1114/terrain/sources/"
CREATE = f"{BASE}/projects/1114/terrain/create-from-bbox/"

GA = {
    "key": "ga_lidar_5m",
    "name": "Geoscience Australia 5 m LiDAR",
    "resolution_m": 5.0,
    "coverage_pct": 100.0,
    "cap": {"state": "ok", "area_km2": 11.0, "max_area_km2": 2500.0, "max_span_deg": 1.0, "message": None},
    "vertical_datum": "AHD",
    "credit": "Geoscience Australia",
    "licence": "CC BY 4.0",
}
COP = {
    "key": "copernicus_glo30",
    "name": "Copernicus GLO-30",
    "resolution_m": 30.0,
    "coverage_pct": 100.0,
    "cap": {"state": "ok", "area_km2": 11.0, "max_area_km2": 40000.0, "max_span_deg": 5.0, "message": None},
    "vertical_datum": "EGM2008",
    "credit": "Copernicus",
    "licence": "Copernicus licence",
}


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _project():
    respx.get(url__regex=r".*/projects/1114/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))


@respx.mock
async def test_sullivans_bbox_lists_ga_5m_first_with_licence_datum_and_caps(_server):
    route = respx.get(SOURCES).mock(return_value=httpx.Response(200, json={"bbox": SULLIVANS, "sources": [GA, COP]}))
    _project()
    data = _tool_json(await _call_as_caller(_server, "list_terrain_sources", {"project_id": 1114, "bbox": SULLIVANS}))
    assert route.calls[0].request.url.params["bbox"] == "149.105,-35.285,149.135,-35.255"
    assert route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert [s["key"] for s in data["sources"]] == ["ga_lidar_5m", "copernicus_glo30"]
    first = data["sources"][0]
    assert first["resolution_m"] == 5.0 and first["licence"] == "CC BY 4.0" and first["vertical_datum"] == "AHD"
    assert first["cap"]["state"] == "ok" and data["outcome"] == "listed"
    assert "Geoscience Australia 5 m" in data["say_to_user"] and data["ui_url"]
    assert data["summary"]["sources"][0]["cap_state"] == "ok"


@respx.mock
async def test_no_coverage_is_plain_words(_server):
    respx.get(SOURCES).mock(return_value=httpx.Response(200, json={"bbox": [1, 1, 2, 2], "sources": []}))
    _project()
    data = _tool_json(
        await _call_as_caller(_server, "list_terrain_sources", {"project_id": 1114, "bbox": [1, 1, 2, 2]})
    )
    assert data["outcome"] == "no_coverage" and "No terrain source" in data["say_to_user"]


@respx.mock
async def test_all_sources_over_cap_says_draw_a_smaller_box(_server):
    big = dict(
        COP,
        cap={
            "state": "too_large",
            "area_km2": 50000.0,
            "max_area_km2": 40000.0,
            "max_span_deg": 5.0,
            "message": "Selected area is 50,000 km2. Please draw a smaller box.",
        },
    )
    respx.get(SOURCES).mock(return_value=httpx.Response(200, json={"bbox": [0, 0, 4, 4], "sources": [big]}))
    _project()
    data = _tool_json(
        await _call_as_caller(_server, "list_terrain_sources", {"project_id": 1114, "bbox": [0, 0, 4, 4]})
    )
    assert "too large" in data["say_to_user"] and "smaller box" in data["say_to_user"]


@respx.mock
async def test_bad_bbox_is_refused_without_a_request(_server):
    route = respx.get(SOURCES).mock(return_value=httpx.Response(200, json={"sources": []}))
    for bad in ([1, 2, 3], [5, 5, 1, 1], ["a", 1, 2, 3]):
        text = _tool_error_text(
            await _call_as_caller(_server, "list_terrain_sources", {"project_id": 1114, "bbox": bad})
        )
        assert text
    assert not route.called


def _terrain(status="ready"):
    return {
        "id": 55,
        "status": status,
        "source": "ga_lidar_5m",
        "dem_elev_min": 540.1,
        "dem_elev_max": 610.2,
        "nodata_fraction": 0.0,
        "native_crs": "EPSG:7855",
        "recorded_vertical_datum": "AHD",
        "gn_layer_name": "ele_1114",
    }


@respx.mock
async def test_create_posts_source_and_bbox_then_polls_and_carries_source_facts(_server):
    post = respx.post(CREATE).mock(return_value=httpx.Response(202, json=_terrain("creating")))
    respx.get(f"{BASE}/projects/1114/terrain/55/").mock(
        side_effect=[httpx.Response(200, json=_terrain("creating")), httpx.Response(200, json=_terrain("ready"))]
    )
    respx.get(SOURCES).mock(return_value=httpx.Response(200, json={"bbox": SULLIVANS, "sources": [GA, COP]}))
    _project()
    data = _tool_json(
        await _call_as_caller(
            _server,
            "create_terrain_from_source",
            {
                "project_id": 1114,
                "bbox": SULLIVANS,
                "source": "ga_lidar_5m",
                "poll_interval_seconds": 0,
                "timeout_seconds": 5,
            },
        )
    )
    assert json.loads(post.calls[0].request.content) == {"source": "ga_lidar_5m", "bbox": SULLIVANS}
    assert data["outcome"] == "ready" and data["polls"] == 2 and data["terrain_id"] == 55
    ts = data["terrain_source"]
    assert (ts["key"], ts["resolution_m"], ts["licence"], ts["vertical_datum"]) == (
        "ga_lidar_5m",
        5.0,
        "CC BY 4.0",
        "AHD",
    )
    assert "5.0 m" in data["say_to_user"] and "CC BY 4.0" in data["say_to_user"] and data["ui_url"]


@respx.mock
async def test_create_refusal_over_cap_is_plain_and_creates_nothing(_server):
    msg = "Selected area is 9,000 km². The maximum is 2,500 km². Please draw a smaller box."
    respx.post(CREATE).mock(return_value=httpx.Response(400, json={"error_code": "VALIDATION_ERROR", "detail": msg}))
    poll = respx.get(f"{BASE}/projects/1114/terrain/").mock(return_value=httpx.Response(200, json=[]))
    _project()
    data = _tool_json(
        await _call_as_caller(
            _server, "create_terrain_from_source", {"project_id": 1114, "bbox": SULLIVANS, "source": "ga_lidar_5m"}
        )
    )
    assert (
        data["outcome"] == "refused" and "9,000" in data["say_to_user"] and "Nothing was created" in data["say_to_user"]
    )
    assert data["ui_url"] and not poll.called


@respx.mock
async def test_create_refusal_no_coverage_is_plain(_server):
    respx.post(CREATE).mock(
        return_value=httpx.Response(
            400,
            json={
                "error_code": "VALIDATION_ERROR",
                "detail": "Geoscience Australia 5 m LiDAR has no data in the selected area. Draw an area it covers, or use Copernicus.",
            },
        )
    )
    _project()
    data = _tool_json(
        await _call_as_caller(
            _server,
            "create_terrain_from_source",
            {"project_id": 1114, "bbox": [10, 10, 10.1, 10.1], "source": "ga_lidar_5m"},
        )
    )
    assert data["outcome"] == "refused" and "has no data" in data["say_to_user"]


@respx.mock
async def test_create_timeout_zero_returns_creating_without_waiting(_server):
    respx.post(CREATE).mock(return_value=httpx.Response(202, json=_terrain("creating")))
    respx.get(f"{BASE}/projects/1114/terrain/55/").mock(return_value=httpx.Response(200, json=_terrain("creating")))
    respx.get(SOURCES).mock(return_value=httpx.Response(200, json={"sources": [GA]}))
    _project()
    data = _tool_json(
        await _call_as_caller(
            _server,
            "create_terrain_from_source",
            {"project_id": 1114, "bbox": SULLIVANS, "source": "ga_lidar_5m", "timeout_seconds": 0},
        )
    )
    assert data["outcome"] == "timed_out" and data["terrain_id"] == 55
