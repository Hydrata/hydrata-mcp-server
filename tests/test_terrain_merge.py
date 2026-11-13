"""TASK-4338 (epic 4329 W1) - merge project terrains into one through the existing analysis-surfaces routes.

merge_terrains creates the recipe (POST analysis-surfaces/), sets the priority stack (POST <pk>/inputs/) and
derives it (POST <pk>/derive/), in that order. The reply carries a pixel estimate, ALWAYS says that gaps in a
higher-priority terrain are filled from the ones below (no Hydrata route reports a void, so it is said every
time), and passes Hydrata's refusal (the over-cap 400) through unchanged. get_terrain_merge polls the recipe
until its merged terrain is ready.
"""

import json
import math

import httpx
import pytest
import respx

from tests.helpers import BASE, CALLER_KEY, _call_as_caller, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

SURFACES = f"{BASE}/projects/42/analysis-surfaces/"
SURFACE = f"{SURFACES}5/"
INPUTS = f"{SURFACES}5/inputs/"
DERIVE = f"{SURFACES}5/derive/"
TERRAINS = f"{BASE}/projects/42/terrain/"
LIDAR = {
    "id": 11,
    "status": "ready",
    "title": "GA 5 m LiDAR",
    "bbox_wgs84": [150.0, -34.0, 150.01, -33.99],
    "native_resolution_m": 5.0,
}
GLO30 = {
    "id": 12,
    "status": "ready",
    "title": "Copernicus 30 m",
    "bbox_wgs84": [150.005, -34.005, 150.02, -33.995],
    "native_resolution_m": 30.0,
}
GAPS = "gaps"


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _expected_pixels(resolution_m, boxes):
    """The union-bbox, metres-per-degree estimate Hydrata's own size guard uses (services_terrain_merge)."""
    w, s = min(b[0] for b in boxes), min(b[1] for b in boxes)
    e, n = max(b[2] for b in boxes), max(b[3] for b in boxes)
    width = (e - w) * 111_320.0 * math.cos(math.radians((s + n) / 2))
    height = (n - s) * 111_320.0
    return math.ceil(width / resolution_m) * math.ceil(height / resolution_m)


def _mock_reads(base_map=77):
    respx.get(TERRAINS).mock(return_value=httpx.Response(200, json=[LIDAR, GLO30]))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": base_map}))


def _mock_writes(derive=None):
    create = respx.post(SURFACES).mock(return_value=httpx.Response(201, json={"id": 5, "title": "Merged"}))
    inputs = respx.post(INPUTS).mock(return_value=httpx.Response(200, json={"id": 5}))
    derive = respx.post(DERIVE).mock(
        return_value=derive
        or httpx.Response(202, json={"detail": "Derive started.", "process_id": "p", "task_id": "t"})
    )
    return create, inputs, derive


ARGS = {"project_id": 42, "terrain_ids": [11, 12], "target_resolution_m": 2.0, "title": "Merged"}


@respx.mock
async def test_merge_terrains_creates_sets_inputs_then_derives_in_that_order(_server):
    _mock_reads()
    create, inputs, derive = _mock_writes()
    extent = [150.001, -34.0, 150.015, -33.991]
    data = _tool_json(await _call_as_caller(_server, "merge_terrains", {**ARGS, "merge_extent_wgs84": extent}))
    writes = [(c.request.method, str(c.request.url)) for c in respx.calls if c.request.method == "POST"]
    assert writes == [("POST", SURFACES), ("POST", INPUTS), ("POST", DERIVE)]
    assert create.calls[0].request.headers["authorization"] == CALLER_KEY
    assert json.loads(create.calls[0].request.content) == {
        "title": "Merged",
        "target_resolution_m": 2.0,
        "merge_extent_wgs84": extent,
    }
    # Priority 0 is the top of the stack; the top terrain is the datum anchor, as the map's own merge does.
    assert json.loads(inputs.calls[0].request.content) == {
        "inputs": [
            {"terrain_id": 11, "priority": 0, "unmodified": True},
            {"terrain_id": 12, "priority": 1, "unmodified": False},
        ]
    }
    assert json.loads(derive.calls[0].request.content) == {"target_resolution_m": 2.0, "merge_extent_wgs84": extent}
    assert data["outcome"] == "started" and data["analysis_surface_id"] == 5
    assert "get_terrain_merge" in data["next_step"]
    assert data["ui_url"].endswith("/catalogue/#/map/77?panel=inputs")


@respx.mock
async def test_merge_terrains_reply_carries_the_pixel_estimate(_server):
    _mock_reads()
    _mock_writes()
    data = _tool_json(await _call_as_caller(_server, "merge_terrains", ARGS))
    expected = _expected_pixels(2.0, [LIDAR["bbox_wgs84"], GLO30["bbox_wgs84"]])
    assert data["summary"]["estimated_pixels"] == expected
    assert data["summary"]["target_resolution_m"] == 2.0
    from hydrata_mcp.terrain import _cells_phrase

    assert _cells_phrase(expected) in data["say_to_user"] and "estimate" in data["say_to_user"]


def test_merge_cells_phrase_never_rounds_a_small_merge_to_zero_million():
    # Live self-verify (epic 4329 W1): a 41,820-cell merge read "about 0.0 million grid cells".
    from hydrata_mcp.terrain import _cells_phrase  # module import needs the test env (HYDRATA_API_URL)

    assert _cells_phrase(41_820) == "about 41,820 grid cells"
    assert _cells_phrase(858_281_255_238) == "about 858,281.3 million grid cells"
    assert _cells_phrase(2_500_000) == "about 2.5 million grid cells"


@respx.mock
async def test_merge_terrains_reply_always_says_gaps_are_filled_from_lower_terrains(_server):
    _mock_reads()
    _mock_writes()
    data = _tool_json(await _call_as_caller(_server, "merge_terrains", ARGS))
    assert GAPS in data["say_to_user"] and "filled from the terrains below" in data["say_to_user"]
    assert "filled from the terrains below" in data["gap_filling"]


@respx.mock
async def test_merge_terrains_over_cap_reply_is_passed_through_unchanged(_server):
    over = {
        "error": "This merge would be too large to build (about 212.4 GB at 0.5 m). Choose a coarser resolution or a smaller area.",
        "estimated_bytes": 212400000000,
        "max_output_bytes": 200000000000,
    }
    _mock_reads()
    _mock_writes(derive=httpx.Response(400, json=over))
    cleanup = respx.delete(SURFACE).mock(return_value=httpx.Response(204))
    data = _tool_json(await _call_as_caller(_server, "merge_terrains", ARGS))
    assert data["outcome"] == "refused" and data["http_status"] == 400
    assert data["hydrata_reply"] == over
    assert over["error"] in data["say_to_user"] and "filled from the terrains below" in data["gap_filling"]
    assert cleanup.call_count == 1  # the recipe made for this call is not left behind


@respx.mock
async def test_merge_terrains_needs_two_terrains_and_sends_nothing(_server):
    _mock_reads()
    create, _inputs, _derive = _mock_writes()
    text = _tool_error_text(await _call_as_caller(_server, "merge_terrains", {**ARGS, "terrain_ids": [11]}))
    assert "two" in text and create.call_count == 0


@respx.mock
async def test_merge_terrains_unknown_terrain_is_refused_before_anything_is_created(_server):
    _mock_reads()
    create, _inputs, _derive = _mock_writes()
    text = _tool_error_text(await _call_as_caller(_server, "merge_terrains", {**ARGS, "terrain_ids": [11, 99]}))
    assert "99" in text and create.call_count == 0


@respx.mock
async def test_get_terrain_merge_polls_the_recipe_until_the_merged_terrain_is_ready(_server):
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    respx.get(SURFACE).mock(
        side_effect=[
            httpx.Response(200, json={"id": 5, "output_terrain": None}),
            httpx.Response(200, json={"id": 5, "output_terrain": 99}),
            httpx.Response(200, json={"id": 5, "output_terrain": 99}),
        ]
    )
    respx.get(f"{TERRAINS}99/").mock(
        side_effect=[
            httpx.Response(200, json={"id": 99, "status": "creating"}),
            httpx.Response(
                200,
                json={"id": 99, "status": "ready", "gn_layer_name": "merged", "dem_elev_min": 1.5, "dem_elev_max": 9.0},
            ),
        ]
    )
    data = _tool_json(
        await _call_as_caller(
            _server, "get_terrain_merge", {"project_id": 42, "analysis_surface_id": 5, "poll_interval_seconds": 0}
        )
    )
    assert data["outcome"] == "ready" and data["terrain_id"] == 99 and data["polls"] == 3
    assert data["summary"]["terrain_id"] == 99 and "ready" in data["say_to_user"]
    assert "layer=geonode%3Amerged" in data["ui_url"] or "layer=geonode:merged" in data["ui_url"]


@respx.mock
async def test_get_terrain_merge_still_merging_is_timed_out_and_says_call_again(_server):
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    respx.get(SURFACE).mock(return_value=httpx.Response(200, json={"id": 5, "output_terrain": None}))
    data = _tool_json(
        await _call_as_caller(
            _server, "get_terrain_merge", {"project_id": 42, "analysis_surface_id": 5, "timeout_seconds": 0}
        )
    )
    assert data["outcome"] == "timed_out" and data["phase"] == "merging" and data["terrain_id"] is None
    assert "get_terrain_merge" in data["next_step"]


@respx.mock
async def test_merge_terrains_refuses_a_terrain_that_is_not_ready_before_anything_is_created(_server):
    """Review fix: Hydrata's derive only checks that a terrain exists; one still importing fails out of sight."""
    respx.get(TERRAINS).mock(return_value=httpx.Response(200, json=[LIDAR, {**GLO30, "status": "creating"}]))
    create, _inputs, _derive = _mock_writes()
    text = _tool_error_text(await _call_as_caller(_server, "merge_terrains", ARGS))
    assert "12" in text and "not ready" in text and create.call_count == 0
