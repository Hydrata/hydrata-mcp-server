"""The scenario's solver (`flow_algorithm`): get_scenario shows it, update_scenario / clone_scenario set it.

DE0 (the default) = standard; DE1 = second-order, about 2x compute and price; DE_ader2 = second-order,
experimental, about 1.25x price. Hydrata validates the value: the tool sends what the agent passed and relays
Hydrata's 400 as it came.
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

DE0_HELP = "DE0 = standard (default)"
DE1_HELP = "DE1 = second-order, about 2x compute and price"
ADER2_HELP = "DE_ader2 = second-order, experimental (still being validated upstream), about 1.25x price"
# What Hydrata answers to an unknown value (the GeoNode V2 envelope).
REFUSAL = {
    "success": False,
    "errors": ["flow_algorithm must be one of DE0 (the default), DE1 or DE_ader2."],
    "code": "invalid",
}


@respx.mock
async def test_get_scenario_returns_flow_algorithm(_server):
    respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(flow_algorithm="DE1")))
    data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
    assert data["flow_algorithm"] == "DE1"


@respx.mock
async def test_get_scenario_from_an_older_hydrata_does_not_invent_it(_server):
    """Absent stays absent (an older server does not send the key): never a fabricated value."""
    respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
    data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
    assert "flow_algorithm" not in data


@respx.mock
@pytest.mark.parametrize("value", ["DE1", "DE_ader2"])
async def test_update_scenario_sets_it(_server, value):
    patch = respx.patch(SCENARIO_URL).mock(return_value=httpx.Response(200, json={}))
    respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(flow_algorithm=value)))
    data = _tool_json(
        await _call_as_caller(_server, "update_scenario", {"project_id": 42, "scenario_id": 5, "flow_algorithm": value})
    )
    assert json.loads(patch.calls[0].request.content) == {"flow_algorithm": value}
    assert data["flow_algorithm"] == value
    assert "Nothing was built" in data["say_to_user"]


@respx.mock
async def test_update_scenario_relays_hydratas_400_verbatim(_server):
    patch = respx.patch(SCENARIO_URL).mock(return_value=httpx.Response(400, json=REFUSAL))
    text = _tool_error_text(
        await _call_as_caller(_server, "update_scenario", {"project_id": 42, "scenario_id": 5, "flow_algorithm": "DE2"})
    )
    assert json.loads(patch.calls[0].request.content) == {"flow_algorithm": "DE2"}  # sent as given
    assert "400" in text
    assert REFUSAL["errors"][0] in text


@respx.mock
async def test_clone_scenario_can_set_it_on_the_copy(_server):
    dup = respx.post(f"{SCENARIO_URL}duplicate/").mock(
        return_value=httpx.Response(201, json={"id": 6, "flow_algorithm": "DE1"})
    )
    respx.get(f"{BASE}/projects/42/scenarios/6/").mock(
        return_value=httpx.Response(200, json=_scenario(id=6, flow_algorithm="DE1"))
    )
    data = _tool_json(
        await _call_as_caller(
            _server,
            "clone_scenario",
            {"project_id": 42, "scenario_id": 5, "overrides": {"flow_algorithm": "DE1"}},
        )
    )
    assert json.loads(dup.calls[0].request.content) == {"flow_algorithm": "DE1"}
    assert data["flow_algorithm"] == "DE1"


async def test_update_scenario_argument_help_says_what_each_value_means(_server):
    help_text = _argument_help((await _tools(_server))["update_scenario"], "flow_algorithm")
    assert DE0_HELP in help_text and DE1_HELP in help_text and ADER2_HELP in help_text


async def test_get_scenario_description_says_what_each_value_means(_server):
    description = (await _tools(_server))["get_scenario"]["description"]
    assert "flow_algorithm" in description
    assert DE0_HELP in description and DE1_HELP in description and ADER2_HELP in description


async def test_clone_scenario_overrides_help_names_it(_server):
    assert "flow_algorithm" in _argument_help((await _tools(_server))["clone_scenario"], "overrides")
