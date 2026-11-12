"""The scenario's terrain sampling (`elevation_sampling`): get_scenario shows it, update_scenario sets it.

footprint (the default) = the area-weighted average of the DEM cells under each triangle; centroid = the DEM
cell at the triangle centre, as a plain ANUGA script reads it. Hydrata validates the value: the tool sends what
the agent passed and relays Hydrata's 400 as it came.
"""

import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    SCENARIO_URL,
    _call_as_caller,
    _scenario,
    _tool_error_text,
    _tool_json,
)
from tests.test_tool_descriptions import _argument_help, _tools

pytestmark = pytest.mark.asyncio

FOOTPRINT_HELP = "footprint = area-weighted average of DEM cells under each triangle (default)"
CENTROID_HELP = "centroid = the DEM cell at the triangle centre (plain-ANUGA style)"
# What Hydrata answers to an unknown value (the GeoNode V2 envelope).
REFUSAL = {
    "success": False,
    "errors": [
        "Terrain sampling must be 'footprint' (the default: the average of the ground under each triangle) "
        "or 'centroid' (the ground at each triangle's centre, as a plain ANUGA script reads it)."
    ],
    "code": "invalid",
}


@respx.mock
async def test_get_scenario_returns_elevation_sampling(_server):
    respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(elevation_sampling="centroid")))
    data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
    assert data["elevation_sampling"] == "centroid"


@respx.mock
async def test_get_scenario_from_an_older_hydrata_does_not_invent_it(_server):
    """Absent stays absent (an older server does not send the key): never a fabricated value."""
    respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
    data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
    assert "elevation_sampling" not in data


@respx.mock
@pytest.mark.parametrize("value", ["centroid", "footprint"])
async def test_update_scenario_sets_it(_server, value):
    patch = respx.patch(SCENARIO_URL).mock(return_value=httpx.Response(200, json={}))
    respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(elevation_sampling=value)))
    data = _tool_json(
        await _call_as_caller(
            _server, "update_scenario", {"project_id": 42, "scenario_id": 5, "elevation_sampling": value}
        )
    )
    assert json.loads(patch.calls[0].request.content) == {"elevation_sampling": value}
    assert data["elevation_sampling"] == value
    assert "Nothing was built" in data["say_to_user"]


@respx.mock
async def test_update_scenario_relays_hydratas_400_verbatim(_server):
    patch = respx.patch(SCENARIO_URL).mock(return_value=httpx.Response(400, json=REFUSAL))
    text = _tool_error_text(
        await _call_as_caller(
            _server, "update_scenario", {"project_id": 42, "scenario_id": 5, "elevation_sampling": "nearest"}
        )
    )
    assert json.loads(patch.calls[0].request.content) == {"elevation_sampling": "nearest"}  # sent as given
    assert "400" in text
    assert REFUSAL["errors"][0] in text


@respx.mock
async def test_clone_scenario_can_set_it_on_the_copy(_server):
    dup = respx.post(f"{SCENARIO_URL}duplicate/").mock(
        return_value=httpx.Response(201, json={"id": 6, "elevation_sampling": "centroid"})
    )
    respx.get(f"{BASE}/projects/42/scenarios/6/").mock(
        return_value=httpx.Response(200, json=_scenario(id=6, elevation_sampling="centroid"))
    )
    data = _tool_json(
        await _call_as_caller(
            _server,
            "clone_scenario",
            {"project_id": 42, "scenario_id": 5, "overrides": {"elevation_sampling": "centroid"}},
        )
    )
    assert json.loads(dup.calls[0].request.content) == {"elevation_sampling": "centroid"}
    assert data["elevation_sampling"] == "centroid"


async def test_update_scenario_argument_help_says_what_each_value_means(_server):
    help_text = _argument_help((await _tools(_server))["update_scenario"], "elevation_sampling")
    assert FOOTPRINT_HELP in help_text and CENTROID_HELP in help_text


async def test_get_scenario_description_says_what_each_value_means(_server):
    description = (await _tools(_server))["get_scenario"]["description"]
    assert "elevation_sampling" in description
    assert FOOTPRINT_HELP in description and CENTROID_HELP in description


async def test_clone_scenario_overrides_help_names_it(_server):
    assert "elevation_sampling" in _argument_help((await _tools(_server))["clone_scenario"], "overrides")
