"""TASK-4061 (epic 4051 W3.3, D9) - the terrain's vertical datum, asked not guessed.

finalize_terrain_upload passes the user's declared datum; get_terrain reports
{declared, recorded, inferred} with recorded > declared > inferred, and when only
Hydrata's own guess exists it tells the agent to ASK the user. It also hands back the
terrain's true elevation range untouched (the range itself is fixed in hydrata).
"""

import importlib
import json

import httpx
import pytest
import respx

from tests.test_server import BASE, CALLER_KEY, _call_as_caller, _tool_error_text, _tool_json

pytestmark = pytest.mark.asyncio

TERRAIN = f"{BASE}/projects/42/terrain/7/"
FINALIZE = f"{BASE}/projects/42/terrain/upload/finalize/"
GUESS = {"datum_guess": "orthometric_egm2008", "confidence": 0.7, "basis": "dod"}


@pytest.fixture
def _server(env_vars):
    import hydrata_mcp.server as srv
    importlib.reload(srv)
    return srv


def _ready(**extra):
    return {"id": 7, "title": "dem", "status": "ready", "gn_layer": 1, "gn_layer_name": "geonode:dem",
            "dem_elev_min": 554.94, "dem_elev_max": 561.37, "metadata": {}, "vertical_datum": None,
            "recorded_vertical_datum": None, **extra}


async def _get(srv, record):
    respx.get(TERRAIN).mock(return_value=httpx.Response(200, json=record))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    return _tool_json(await _call_as_caller(
        srv, "get_terrain", {"project_id": 42, "terrain_id": 7, "poll_interval_seconds": 0}))


@respx.mock
async def test_finalize_with_ahd_sends_it_as_vertical_datum_declared(_server):
    route = respx.post(FINALIZE).mock(return_value=httpx.Response(202, json={"id": 7, "status": "creating"}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    await _call_as_caller(_server, "finalize_terrain_upload", {
        "project_id": 42, "staging_key": "k", "process_id": "p", "vertical_datum_declared": "AHD"})
    assert route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert json.loads(route.calls[0].request.content)["vertical_datum_declared"] == "ahd"


@respx.mock
async def test_finalize_without_a_datum_sends_no_datum_key(_server):
    route = respx.post(FINALIZE).mock(return_value=httpx.Response(202, json={"id": 7, "status": "creating"}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    await _call_as_caller(_server, "finalize_terrain_upload", {"project_id": 42, "staging_key": "k"})
    assert "vertical_datum_declared" not in json.loads(route.calls[0].request.content)


@respx.mock
async def test_finalize_refuses_an_unknown_datum_without_calling_the_api(_server):
    route = respx.post(FINALIZE).mock(return_value=httpx.Response(202, json={}))
    text = _tool_error_text(await _call_as_caller(_server, "finalize_terrain_upload", {
        "project_id": 42, "staging_key": "k", "vertical_datum_declared": "navd88"}))
    assert "ahd" in text and "ask them" in text
    assert route.call_count == 0


@respx.mock
async def test_declared_datum_is_reported_and_effective(_server):
    data = await _get(_server, _ready(metadata={"vertical_datum_declared": "ahd"}, vertical_datum=GUESS))
    datum = data["vertical_datum"]
    assert datum["declared"] == "ahd" and datum["recorded"] is None
    assert datum["inferred"]["datum"] == "orthometric_egm2008"
    assert datum["effective"] == "ahd" and datum["effective_source"] == "declared"
    assert "ask the user" not in data["say_to_user"].lower()


@respx.mock
async def test_recorded_beats_declared_beats_inferred(_server):
    rec = {"value": "EPSG:5711", "label": "AHD height (EPSG:5711)", "source": "editor"}
    data = await _get(_server, _ready(
        metadata={"vertical_datum_declared": "ellipsoid", "vertical_datum_recorded": "EPSG:5711"},
        vertical_datum=GUESS, recorded_vertical_datum=rec))
    datum = data["vertical_datum"]
    assert (datum["recorded"], datum["declared"]) == ("EPSG:5711", "ellipsoid")
    assert datum["effective"] == "EPSG:5711" and datum["effective_source"] == "recorded"


@respx.mock
async def test_only_inferred_tells_the_agent_to_ask_the_user_not_to_relay_the_guess(_server):
    data = await _get(_server, _ready(vertical_datum=GUESS))
    datum = data["vertical_datum"]
    assert datum["declared"] is None and datum["recorded"] is None
    assert datum["effective_source"] == "inferred"
    say = data["say_to_user"]
    assert "Ask the user which vertical datum" in say and "guess" in say and "not tell" in say


@respx.mock
async def test_no_datum_at_all_asks_the_user(_server):
    data = await _get(_server, _ready())
    assert data["vertical_datum"]["effective"] is None
    assert "ask the user" in data["say_to_user"].lower()


@respx.mock
async def test_elevation_range_is_passed_through_exactly(_server):
    data = await _get(_server, _ready(metadata={"vertical_datum_declared": "ahd"}))
    assert data["terrain"]["dem_elev_min"] == 554.94 and data["summary"]["dem_elev_min"] == 554.94
    assert "554.94 to 561.37" in data["say_to_user"]


@respx.mock
async def test_an_unknown_inference_is_no_inference_and_asks_the_user(_server):
    """Hydrata's own check returns datum_guess 'unknown' when it cannot tell: that is not a guess to relay."""
    data = await _get(_server, _ready(vertical_datum={"datum_guess": "unknown", "confidence": 0.0, "basis": None}))
    datum = data["vertical_datum"]
    assert datum["inferred"] is None and datum["effective"] is None
    assert "guesses" not in data["say_to_user"] and "ask the user" in data["say_to_user"].lower()
