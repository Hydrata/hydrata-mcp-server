"""TASK-4108 (epic 4117 W7e) - sample_terrain and terrain_profile: read the ground before placing on it."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from tests.helpers import BASE, _call_as_caller, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

SAMPLE = f"{BASE}/projects/42/terrain/7/sample/"
PROFILE = f"{BASE}/projects/42/terrain/7/profile/"


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _query(route) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(str(route.calls[0].request.url)).query).items()}


@respx.mock
async def test_sample_terrain_sends_a_multipoint_and_relays_levels_with_nulls(_server):
    route = respx.get(SAMPLE).mock(
        return_value=httpx.Response(
            200,
            json={
                "samples": [
                    {"lon": 149.1, "lat": -35.28, "terrain": 567.04321},
                    {"lon": 10.0, "lat": 10.0, "terrain": None},
                ],
                "crs": "EPSG:4326",
            },
        )
    )
    data = _tool_json(
        await _call_as_caller(
            _server, "sample_terrain", {"project_id": 42, "terrain_id": 7, "points": [[149.1, -35.28], [10, 10]]}
        )
    )
    assert _query(route)["points"] == "MULTIPOINT((149.1 -35.28), (10.0 10.0))"
    assert data["samples"] == [
        {"lon": 149.1, "lat": -35.28, "level": 567.043},
        {"lon": 10.0, "lat": 10.0, "level": None},
    ]
    assert data["summary"]["with_level"] == 1 and data["summary"]["without_level"] == 1
    assert "no ground level" in data["say_to_user"]


@respx.mock
async def test_sample_terrain_refuses_bad_points_before_any_request(_server):
    route = respx.get(SAMPLE).mock(return_value=httpx.Response(200, json={"samples": []}))
    for bad in ([], [[1]], [[149.1, "x"]], [[500, 0]], [[0, 95]], [[0, 0]] * 501):
        text = _tool_error_text(
            await _call_as_caller(_server, "sample_terrain", {"project_id": 42, "terrain_id": 7, "points": bad})
        )
        assert "Nothing was sent" in text or "points" in text, bad
    assert route.call_count == 0


@respx.mock
async def test_terrain_profile_returns_levels_lowest_and_summary(_server):
    route = respx.get(PROFILE).mock(
        return_value=httpx.Response(
            200,
            json={
                "samples": [
                    {"distance_m": 0.0, "lon": 149.1, "lat": -35.28, "dem": 570.5},
                    {"distance_m": 5.004, "lon": 149.1001, "lat": -35.28, "dem": 567.25},
                    {"distance_m": 10.0, "lon": 149.1002, "lat": -35.28, "dem": None},
                    {"distance_m": 15.0, "lon": 149.1003, "lat": -35.28, "dem": 569.0},
                ],
                "crs": "EPSG:4326",
            },
        )
    )
    data = _tool_json(
        await _call_as_caller(
            _server,
            "terrain_profile",
            {"project_id": 42, "terrain_id": 7, "line": [[149.1, -35.28], [149.1003, -35.28]], "step_m": 5},
        )
    )
    query = _query(route)
    assert query["line"] == "LINESTRING(149.1 -35.28, 149.1003 -35.28)" and float(query["spacing_m"]) == 5.0
    assert [row["level"] for row in data["profile"]] == [570.5, 567.25, None, 569.0]
    assert data["lowest"] == {"distance_m": 5.0, "lon": 149.1001, "lat": -35.28, "level": 567.25}
    assert data["summary"]["without_level"] == 1 and data["summary"]["level_max"] == 570.5
    assert "invert candidate" in data["say_to_user"]


@respx.mock
async def test_terrain_profile_off_the_terrain_has_no_lowest_and_says_so(_server):
    respx.get(PROFILE).mock(
        return_value=httpx.Response(200, json={"samples": [{"distance_m": 0.0, "lon": 1, "lat": 1, "dem": None}] * 2})
    )
    data = _tool_json(
        await _call_as_caller(
            _server, "terrain_profile", {"project_id": 42, "terrain_id": 7, "line": [[1, 1], [1.001, 1]]}
        )
    )
    assert data["lowest"] is None and "no ground level" in data["say_to_user"]


@respx.mock
async def test_terrain_profile_refuses_a_one_point_line_and_a_bad_step(_server):
    route = respx.get(PROFILE).mock(return_value=httpx.Response(200, json={"samples": []}))
    for args in ({"line": [[1, 1]]}, {"line": [[1, 1], [2, 2]], "step_m": 0}, {"line": [[1, 1], [2, 2]], "step_m": -3}):
        text = _tool_error_text(
            await _call_as_caller(_server, "terrain_profile", {"project_id": 42, "terrain_id": 7, **args})
        )
        assert "Nothing was sent" in text
    assert route.call_count == 0


async def test_sample_terrain_and_terrain_profile_are_read_only_tools_with_a_snapshot_entry(_server):
    tools = {t.name: t for t in await _server.mcp.list_tools()}
    for name in ("sample_terrain", "terrain_profile"):
        assert tools[name].annotations.readOnlyHint is True and tools[name].annotations.destructiveHint is False
    snapshot = json.load(open("tests/snapshots/tools.json"))
    names = {t["name"] for t in (snapshot if isinstance(snapshot, list) else snapshot["tools"])}
    assert {"sample_terrain", "terrain_profile"} <= names


@respx.mock
async def test_sample_terrain_sends_more_than_100_points_in_chunks_and_keeps_the_order(_server):
    """W7e review F1: nginx holds an 8 KB request line (about 180 points), so the tool sends chunks of 100."""
    seen = []

    def answer(request):
        wkt = parse_qs(urlparse(str(request.url)).query)["points"][0]
        pts = wkt[len("MULTIPOINT(") : -1].split(", ")
        seen.append(len(pts))
        return httpx.Response(
            200,
            json={"samples": [{"lon": float(p.strip("()").split()[0]), "lat": 0.0, "terrain": 1.0} for p in pts]},
        )

    route = respx.get(SAMPLE).mock(side_effect=answer)
    points = [[i / 10, 0.0] for i in range(1, 251)]
    data = _tool_json(
        await _call_as_caller(_server, "sample_terrain", {"project_id": 42, "terrain_id": 7, "points": points})
    )
    assert route.call_count == 3 and seen == [100, 100, 50]
    assert [row["lon"] for row in data["samples"]] == [i / 10 for i in range(1, 251)]
    assert data["summary"]["points"] == 250 and data["summary"]["with_level"] == 250


@respx.mock
async def test_terrain_profile_refuses_a_line_that_would_return_more_than_500_samples_before_any_request(_server):
    """W7e review F8: a 10 km line at 5 m is 2001 rows; the tool says which step fits instead of flooding the agent."""
    route = respx.get(PROFILE).mock(return_value=httpx.Response(200, json={"samples": []}))
    text = _tool_error_text(
        await _call_as_caller(
            _server,
            "terrain_profile",
            {"project_id": 42, "terrain_id": 7, "line": [[149.0, -35.28], [149.11, -35.28]], "step_m": 5},
        )
    )
    assert "500" in text and "step_m of at least" in text and "Nothing was sent" in text
    assert not route.called


@respx.mock
async def test_terrain_profile_reports_the_spacing_actually_used(_server):
    """W7e review F8: Hydrata fits whole steps to the line, so summary.step_m is the spacing of the rows."""
    rows = [{"distance_m": d, "lon": 149.1, "lat": -35.28, "dem": 10.0 + d} for d in (0.0, 4.8, 9.6, 14.4)]
    respx.get(PROFILE).mock(return_value=httpx.Response(200, json={"samples": rows}))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "terrain_profile",
            {"project_id": 42, "terrain_id": 7, "line": [[149.1, -35.28], [149.1002, -35.28]], "step_m": 5},
        )
    )
    assert data["summary"]["step_m"] == 4.8 and data["summary"]["step_m_requested"] == 5
