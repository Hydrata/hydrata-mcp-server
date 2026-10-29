"""TASK-3208 (epic 3200 W2b; absorbs TASK-3209) — the MCP relay of input review.

Hydrata (not this server) refuses to build or run while an input the agent wrote
is unreviewed: 409 UNREVIEWED_INPUTS, and a run built from inputs other than the
ones reviewed now is 409 RUN_INVALIDATED. A person reviews in the browser, on
the scenario's review screen. This server relays each refusal as a normal
(non-error) result keyed on ``error_code`` — never on the status code alone:
``outcome`` ``unreviewed_inputs`` with ``ui_url`` ``…?panel=input-review&scenario=<id>``
and the unreviewed layers in ``say_to_user`` — and it never waits for the person
inside a call: the agent polls get_scenario, whose compact record carries
``unreviewed_inputs[]``. The inflow rule (from 3209): never invent an inflow
location.
"""

import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    BUILD_URL,
    SCENARIO_URL,
    _call_as_caller,
    _mock_boundary,
    _scenario,
    _tool_json,
    reload_server,
)

pytestmark = pytest.mark.asyncio

PROJECT_URL = f"{BASE}/projects/42/"
UNREVIEWED = [
    {
        "kind": "boundary",
        "layer_id": 27497,
        "title": "Boundary 01",
        "version": "a" * 64,
        "readable": True,
        "grant": "Claude Desktop",
        "grant_level": "launch",
        "client": "hydrata-mcp/1.0",
        "gn_layer_name": "geonode:bdy_42_boundary_01",
        "feature_count": 4,
        "reopened": False,
    },
    {
        "kind": "friction",
        "layer_id": 124557,
        "title": "Friction 01",
        "version": "b" * 64,
        "readable": True,
        "grant": "Claude Desktop",
        "grant_level": "launch",
        "client": "hydrata-mcp/1.0",
        "gn_layer_name": "geonode:fri_42_friction_01",
        "feature_count": 2,
        "reopened": True,
    },
]
UNREVIEWED_409 = {
    "error_code": "UNREVIEWED_INPUTS",
    "detail": "Inputs written by an agent have not been reviewed.",
    "scenario_id": 5,
    "project_id": 42,
    "unreviewed_inputs": UNREVIEWED,
}
INVALIDATED_409 = {
    "error_code": "RUN_INVALIDATED",
    "detail": "This run was built from inputs that are not the ones reviewed now.",
    "scenario_id": 5,
    "project_id": 42,
    "run_id": 501,
    "changed_inputs": ["boundary"],
}


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _mock_project():
    return respx.get(PROJECT_URL).mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 7}))


async def _build(srv):
    return await _call_as_caller(
        srv, "build_scenario", {"project_id": 42, "scenario_id": 5, "poll_interval_seconds": 0}
    )


class TestBuildRelay:
    @respx.mock
    async def test_a_refused_build_returns_unreviewed_inputs_with_the_review_link(self, _server):
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        _mock_project()
        build = respx.post(BUILD_URL).mock(return_value=httpx.Response(409, json=UNREVIEWED_409))
        data = _tool_json(await _build(_server))  # a normal result, not isError
        assert build.call_count == 1
        assert data["outcome"] == "unreviewed_inputs"
        assert data["error_code"] == "UNREVIEWED_INPUTS" and data["http_status"] == 409
        assert data["unreviewed_inputs"] == UNREVIEWED
        assert data["ui_url"] == "https://hydrata.example.com/catalogue/#/map/7?panel=input-review&scenario=5"
        assert "Boundary 01" in data["say_to_user"] and "Friction 01" in data["say_to_user"]
        assert "review" in data["say_to_user"].lower()
        assert "get_scenario" in data["next_step"] and "unreviewed_inputs" in data["next_step"]

    @respx.mock
    async def test_the_refusal_does_not_poll_or_wait_for_the_person(self, _server):
        detail = respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        _mock_project()
        respx.post(BUILD_URL).mock(return_value=httpx.Response(409, json=UNREVIEWED_409))
        await _build(_server)
        assert detail.call_count == 1  # the pre-check read only: no poll loop

    @respx.mock
    async def test_the_outcome_is_keyed_on_error_code_not_on_the_409(self, _server):
        """NEGATIVE CONTROL: another 409 with an error_code is relayed as before, no review link."""
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        _mock_project()
        other = {"error_code": "COMPUTE_TARGET_UNAVAILABLE", "detail": "misconfigured"}
        respx.post(BUILD_URL).mock(return_value=httpx.Response(409, json=other))
        data = _tool_json(await _build(_server))
        assert data.get("outcome") != "unreviewed_inputs"
        assert "panel=input-review" not in (data["ui_url"] or "")


class TestRunRelay:
    @respx.mock
    async def test_a_refused_run_returns_unreviewed_inputs_with_the_review_link(self, _server):
        _mock_project()
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(409, json=UNREVIEWED_409))
        data = _tool_json(await _call_as_caller(_server, "start_simulation", {"scenario_id": 5}))
        assert data["outcome"] == "unreviewed_inputs" and data["error_code"] == "UNREVIEWED_INPUTS"
        assert data["ui_url"] == "https://hydrata.example.com/catalogue/#/map/7?panel=input-review&scenario=5"
        assert "Boundary 01" in data["say_to_user"]

    @respx.mock
    async def test_an_invalidated_run_says_rebuild(self, _server):
        _mock_project()
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(409, json=INVALIDATED_409))
        data = _tool_json(await _call_as_caller(_server, "start_simulation", {"scenario_id": 5}))
        assert data["outcome"] == "run_invalidated" and data["error_code"] == "RUN_INVALIDATED"
        assert data["changed_inputs"] == ["boundary"]
        assert "build_scenario" in data["next_step"] and "rebuild=true" in data["next_step"]
        assert data["ui_url"] == "https://hydrata.example.com/catalogue/#/map/7?panel=scenarios&scenario=5"

    @respx.mock
    async def test_another_run_409_still_raises(self, _server):
        """NEGATIVE CONTROL: INVALID_TRANSITION stays an error, as before."""
        respx.post(f"{BASE}/scenarios/5/run/").mock(
            return_value=httpx.Response(
                409, json={"error_code": "INVALID_TRANSITION", "detail": "Scenario has no runs."}
            )
        )
        result = await _call_as_caller(_server, "start_simulation", {"scenario_id": 5})
        assert result.get("isError") is True


class TestGetScenarioCarriesUnreviewedInputs:
    @respx.mock
    async def test_get_scenario_compact_record_carries_unreviewed_inputs(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(
            return_value=httpx.Response(200, json=_scenario(unreviewed_inputs=UNREVIEWED))
        )
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        assert data["unreviewed_inputs"] == UNREVIEWED

    @respx.mock
    async def test_a_stranger_read_has_no_unreviewed_inputs_key(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(
            return_value=httpx.Response(200, json={"id": 5, "name": "x", "latest_run": None})
        )
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        assert "unreviewed_inputs" not in data  # withheld is not "nothing to review"


class TestInflowRuleAndInstructions:
    async def test_attach_input_layer_and_the_instructions_carry_the_inflow_rule(self, _server):
        for text in (_server.attach_input_layer.__doc__, _server.mcp.instructions):
            assert "never invent" in text.lower() and "inflow" in text.lower()

    async def test_the_instructions_say_the_person_reviews_and_the_agent_polls(self, _server):
        text = _server.mcp.instructions
        assert "UNREVIEWED_INPUTS" in text and "reviews them" in text
        assert "get_scenario" in text and "unreviewed_inputs" in text
