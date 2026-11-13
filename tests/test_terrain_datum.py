"""TASK-4061 (epic 4051 W3.3, D9) - the terrain's vertical datum, asked not guessed.

finalize_terrain_upload passes the user's declared datum; get_terrain reports
{declared, recorded, inferred} with recorded > declared > inferred, and when only
Hydrata's own guess exists it tells the agent to ASK the user. It also hands back the
terrain's true elevation range untouched (the range itself is fixed in hydrata).
"""

import json

import httpx
import pytest
import respx

from tests.helpers import BASE, CALLER_KEY, _call_as_caller, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

TERRAIN = f"{BASE}/projects/42/terrain/7/"
FINALIZE = f"{BASE}/projects/42/terrain/upload/finalize/"
GUESS = {"datum_guess": "orthometric_egm2008", "confidence": 0.7, "basis": "dod"}


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _ready(**extra):
    return {
        "id": 7,
        "title": "dem",
        "status": "ready",
        "gn_layer": 1,
        "gn_layer_name": "geonode:dem",
        "dem_elev_min": 554.94,
        "dem_elev_max": 561.37,
        "metadata": {},
        "vertical_datum": None,
        "recorded_vertical_datum": None,
        **extra,
    }


async def _get(srv, record):
    respx.get(TERRAIN).mock(return_value=httpx.Response(200, json=record))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    return _tool_json(
        await _call_as_caller(srv, "get_terrain", {"project_id": 42, "terrain_id": 7, "poll_interval_seconds": 0})
    )


@respx.mock
async def test_finalize_with_ahd_sends_it_as_vertical_datum_declared(_server):
    route = respx.post(FINALIZE).mock(return_value=httpx.Response(202, json={"id": 7, "status": "creating"}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    await _call_as_caller(
        _server,
        "finalize_terrain_upload",
        {"project_id": 42, "staging_key": "k", "process_id": "p", "vertical_datum_declared": "AHD"},
    )
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
    text = _tool_error_text(
        await _call_as_caller(
            _server,
            "finalize_terrain_upload",
            {"project_id": 42, "staging_key": "k", "vertical_datum_declared": "navd88"},
        )
    )
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
    data = await _get(
        _server,
        _ready(
            metadata={"vertical_datum_declared": "ellipsoid", "vertical_datum_recorded": "EPSG:5711"},
            vertical_datum=GUESS,
            recorded_vertical_datum=rec,
        ),
    )
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


@respx.mock
async def test_finalize_levels_datum_is_sent_beside_not_as_the_terrain_datum(_server):
    """TASK-4118 (D32): the person's levels datum is its own field; vertical_datum_declared is untouched."""
    route = respx.post(FINALIZE).mock(return_value=httpx.Response(202, json={"id": 7, "status": "creating"}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    await _call_as_caller(
        _server, "finalize_terrain_upload", {"project_id": 42, "staging_key": "k", "levels_datum": " NAVD88 "}
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["levels_datum"] == "NAVD88" and "vertical_datum_declared" not in sent


@respx.mock
async def test_finalize_without_levels_datum_sends_no_levels_datum_key(_server):
    route = respx.post(FINALIZE).mock(return_value=httpx.Response(202, json={"id": 7, "status": "creating"}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    await _call_as_caller(_server, "finalize_terrain_upload", {"project_id": 42, "staging_key": "k"})
    assert "levels_datum" not in json.loads(route.calls[0].request.content)


@respx.mock
async def test_get_terrain_levels_datum_and_mismatch_are_relayed(_server):
    levels = {"datum": "NAVD88", "label": "NAVD88", "declared_by": "dave", "declared_at": None}
    sentence = "Your levels are on NAVD88 but this terrain is on AHD height (EPSG:5711)."
    data = await _get(
        _server,
        _ready(metadata={"vertical_datum_declared": "ahd"}, levels_datum=levels, levels_datum_mismatch=sentence),
    )
    assert data["levels_datum"] == levels
    assert sentence in data["say_to_user"] and data["vertical_datum"]["effective"] == "ahd"


@respx.mock
async def test_get_terrain_without_a_levels_datum_asks_the_user_for_it(_server):
    data = await _get(_server, _ready(metadata={"vertical_datum_declared": "ahd"}, levels_datum=None))
    assert data["levels_datum"] is None and "no levels datum" in data["say_to_user"].lower()


@respx.mock
async def test_get_terrain_levels_datum_drops_the_inferred_guess_when_the_basis_is_declared(_server):
    declared = {
        "datum_guess": "orthometric_ahd",
        "basis": "declared",
        "confidence": None,
        "inferred_datum_guess": "orthometric_egm2008",
        "inferred_confidence": 0.559,
    }
    data = await _get(_server, _ready(metadata={"vertical_datum_declared": "ahd"}, vertical_datum=declared))
    relayed = data["terrain"]["vertical_datum"]
    assert "inferred_datum_guess" not in relayed and "inferred_confidence" not in relayed
    assert relayed["datum_guess"] == "orthometric_ahd"


@respx.mock
async def test_get_terrain_levels_datum_keeps_the_inferred_guess_when_nothing_is_declared(_server):
    only_guess = {
        "datum_guess": "orthometric_egm2008",
        "basis": "dod",
        "confidence": 0.7,
        "inferred_datum_guess": "orthometric_egm2008",
    }
    data = await _get(_server, _ready(vertical_datum=only_guess))
    assert data["terrain"]["vertical_datum"]["inferred_datum_guess"] == "orthometric_egm2008"


# ---------------------------------------------------------------------------
# TASK-4336 (epic 4329 W1): the ellipsoid to EGM2008 shift, on finalize and as its own tool.
# ---------------------------------------------------------------------------
CONVERT = f"{BASE}/projects/42/terrain/7/convert-datum/"


@respx.mock
async def test_finalize_convert_to_egm2008_sends_the_flag_with_an_ellipsoid_declaration(_server):
    route = respx.post(FINALIZE).mock(return_value=httpx.Response(202, json={"id": 7, "status": "creating"}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "finalize_terrain_upload",
            {"project_id": 42, "staging_key": "k", "vertical_datum_declared": "ellipsoid", "convert_to_egm2008": True},
        )
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["convert_to_egm2008"] is True and sent["vertical_datum_declared"] == "ellipsoid"
    assert "EGM2008" in data["say_to_user"]


@respx.mock
async def test_finalize_without_convert_to_egm2008_sends_no_flag(_server):
    route = respx.post(FINALIZE).mock(return_value=httpx.Response(202, json={"id": 7, "status": "creating"}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 7}))
    await _call_as_caller(
        _server,
        "finalize_terrain_upload",
        {"project_id": 42, "staging_key": "k", "vertical_datum_declared": "ellipsoid"},
    )
    assert "convert_to_egm2008" not in json.loads(route.calls[0].request.content)


@respx.mock
async def test_finalize_convert_to_egm2008_without_an_ellipsoid_declaration_is_refused_before_the_api(_server):
    """Hydrata honours the flag only for an ellipsoid declaration (it would double-shift anything else)."""
    route = respx.post(FINALIZE).mock(return_value=httpx.Response(202, json={}))
    text = _tool_error_text(
        await _call_as_caller(
            _server,
            "finalize_terrain_upload",
            {"project_id": 42, "staging_key": "k", "vertical_datum_declared": "ahd", "convert_to_egm2008": True},
        )
    )
    assert "ellipsoid" in text and route.call_count == 0


@respx.mock
async def test_convert_terrain_datum_posts_to_convert_datum_and_hands_back(_server):
    route = respx.post(CONVERT).mock(
        return_value=httpx.Response(202, json={"id": 9, "title": "dem (EGM2008)", "status": "creating"})
    )
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    data = _tool_json(await _call_as_caller(_server, "convert_terrain_datum", {"project_id": 42, "terrain_id": 7}))
    assert route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert data["outcome"] == "started" and data["terrain_id"] == 9 and data["source_terrain_id"] == 7
    assert data["ui_url"].endswith("/catalogue/#/map/77?panel=inputs")
    assert data["summary"]["terrain_id"] == 9 and data["summary"]["source_terrain_id"] == 7
    assert "get_terrain" in data["next_step"] and "EGM2008" in data["say_to_user"]


@respx.mock
async def test_convert_terrain_datum_existing_conversion_is_reported_not_duplicated(_server):
    respx.post(CONVERT).mock(return_value=httpx.Response(200, json={"id": 9, "status": "ready"}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    data = _tool_json(await _call_as_caller(_server, "convert_terrain_datum", {"project_id": 42, "terrain_id": 7}))
    assert data["outcome"] == "already_converted" and data["terrain_id"] == 9
    assert "already" in data["say_to_user"]


@respx.mock
async def test_convert_terrain_datum_refusal_is_relayed_verbatim(_server):
    detail = (
        "This terrain is already on EGM2008 orthometric height; converting it again would double-apply the geoid shift."
    )
    respx.post(CONVERT).mock(return_value=httpx.Response(409, json={"error_code": "ALREADY_EGM2008", "detail": detail}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    data = _tool_json(await _call_as_caller(_server, "convert_terrain_datum", {"project_id": 42, "terrain_id": 7}))
    assert data["outcome"] == "refused" and data["http_status"] == 409
    assert data["error_code"] == "ALREADY_EGM2008" and data["detail"] == detail
    assert detail in data["say_to_user"] and "Nothing was created" in data["say_to_user"]


# ---------------------------------------------------------------------------
# TASK-4337 (epic 4329 W1, D7): record the person's levels datum on a project that already has a terrain.
# ---------------------------------------------------------------------------
LEVELS = f"{BASE}/projects/42/levels-datum/"
AHD_LEVELS = {"datum": "EPSG:5711", "label": "AHD height (EPSG:5711)", "declared_by": "dave", "declared_at": None}


@respx.mock
async def test_set_levels_datum_patches_the_project_route_and_hands_back(_server):
    route = respx.patch(LEVELS).mock(
        return_value=httpx.Response(
            200, json={"project": 42, "levels_datum": AHD_LEVELS, "levels_datum_mismatch": None, "terrain": 7}
        )
    )
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    data = _tool_json(await _call_as_caller(_server, "set_levels_datum", {"project_id": 42, "levels_datum": " AHD "}))
    assert route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert json.loads(route.calls[0].request.content) == {"levels_datum": "AHD"}
    assert data["levels_datum"] == AHD_LEVELS and data["summary"]["levels_datum"] == AHD_LEVELS
    assert data["ui_url"].endswith("/catalogue/#/map/77?panel=inputs")
    assert "AHD height (EPSG:5711)" in data["say_to_user"] and "WARNING" not in data["say_to_user"]


@respx.mock
async def test_set_levels_datum_mismatch_with_the_terrain_is_relayed(_server):
    sentence = "Your levels are on NAVD88 but this terrain is on AHD height (EPSG:5711): fix it."
    respx.patch(LEVELS).mock(
        return_value=httpx.Response(
            200,
            json={
                "project": 42,
                "levels_datum": {**AHD_LEVELS, "datum": "NAVD88", "label": "NAVD88"},
                "levels_datum_mismatch": sentence,
                "terrain": 7,
            },
        )
    )
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    data = _tool_json(await _call_as_caller(_server, "set_levels_datum", {"project_id": 42, "levels_datum": "NAVD88"}))
    assert data["levels_datum_mismatch"] == sentence and data["summary"]["levels_datum_mismatch"] == sentence
    assert "WARNING" in data["say_to_user"] and sentence in data["say_to_user"]


@respx.mock
async def test_set_levels_datum_without_a_datum_is_refused_before_the_api(_server):
    route = respx.patch(LEVELS).mock(return_value=httpx.Response(200, json={}))
    text = _tool_error_text(
        await _call_as_caller(_server, "set_levels_datum", {"project_id": 42, "levels_datum": "  "})
    )
    assert "ask them" in text and route.call_count == 0


@respx.mock
async def test_set_levels_datum_hydrata_refusal_carries_its_detail(_server):
    respx.patch(LEVELS).mock(
        return_value=httpx.Response(
            400, json={"error_code": "VALIDATION_ERROR", "detail": "levels_datum: EPSG:4326 is not a vertical CRS"}
        )
    )
    text = _tool_error_text(
        await _call_as_caller(_server, "set_levels_datum", {"project_id": 42, "levels_datum": "EPSG:4326"})
    )
    assert "VALIDATION_ERROR" in text and "not a vertical CRS" in text


@respx.mock
async def test_get_terrain_without_a_levels_datum_names_set_levels_datum(_server):
    """D7: the terrain exists, so the agent records the levels datum on the project, not by fetching terrain again."""
    data = await _get(_server, _ready(metadata={"vertical_datum_declared": "ahd"}, levels_datum=None))
    say = data["say_to_user"]
    assert "set_levels_datum" in say and "create_terrain_from_source" not in say
