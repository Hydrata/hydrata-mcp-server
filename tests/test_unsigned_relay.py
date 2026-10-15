"""TASK-3208 (epic 3200 W2b; absorbs TASK-3209) — the MCP relay of input sign-off.

Hydrata (not this server) refuses to build or run while an input the agent wrote
is unsigned: 409 UNSIGNED_INPUTS, and a run built from inputs other than the
ones signed now is 409 RUN_INVALIDATED. A person signs off in the browser, on
the scenario's review screen. This server relays each refusal as a normal
(non-error) result keyed on ``error_code`` — never on the status code alone:
``outcome`` ``unsigned_inputs`` with ``ui_url`` ``…?panel=review&scenario=<id>``
and the unsigned layers in ``say_to_user`` — and it never waits for the person
inside a call: the agent polls get_scenario, whose compact record carries
``unsigned_inputs[]``. The inflow rule (from 3209): never invent an inflow
location.
"""
import importlib
import json

import httpx
import pytest
import respx

from tests.test_server import (
    BASE,
    BUILD_URL,
    SCENARIO_URL,
    _call_as_caller,
    _mock_boundary,
    _scenario,
    _tool_json,
)

pytestmark = pytest.mark.asyncio

PROJECT_URL = f"{BASE}/projects/42/"
UNSIGNED = [
    {"kind": "boundary", "layer_id": 27497, "title": "Boundary 01", "version": "a" * 64, "readable": True,
     "grant": "Claude Desktop", "grant_level": "launch", "client": "hydrata-mcp/1.0",
     "gn_layer_name": "geonode:bdy_42_boundary_01", "feature_count": 4, "reopened": False},
    {"kind": "friction", "layer_id": 124557, "title": "Friction 01", "version": "b" * 64, "readable": True,
     "grant": "Claude Desktop", "grant_level": "launch", "client": "hydrata-mcp/1.0",
     "gn_layer_name": "geonode:fri_42_friction_01", "feature_count": 2, "reopened": True},
]
UNSIGNED_409 = {
    "error_code": "UNSIGNED_INPUTS",
    "detail": "Inputs written by an agent have not been signed off.",
    "scenario_id": 5,
    "project_id": 42,
    "unsigned_inputs": UNSIGNED,
}
INVALIDATED_409 = {
    "error_code": "RUN_INVALIDATED",
    "detail": "This run was built from inputs that are not the ones signed off now.",
    "scenario_id": 5,
    "project_id": 42,
    "run_id": 501,
    "changed_inputs": ["boundary"],
}


@pytest.fixture
def _server(env_vars):
    import hydrata_mcp.server as srv
    importlib.reload(srv)
    return srv


def _mock_project():
    return respx.get(PROJECT_URL).mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 7}))


async def _build(srv):
    return await _call_as_caller(srv, "build_scenario", {"project_id": 42, "scenario_id": 5,
                                                         "poll_interval_seconds": 0})


class TestBuildRelay:
    @respx.mock
    async def test_a_refused_build_returns_unsigned_inputs_with_the_review_link(self, _server):
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        _mock_project()
        build = respx.post(BUILD_URL).mock(return_value=httpx.Response(409, json=UNSIGNED_409))
        data = _tool_json(await _build(_server))      # a normal result, not isError
        assert build.call_count == 1
        assert data["outcome"] == "unsigned_inputs"
        assert data["error_code"] == "UNSIGNED_INPUTS" and data["http_status"] == 409
        assert data["unsigned_inputs"] == UNSIGNED
        assert data["ui_url"] == "https://hydrata.example.com/catalogue/#/map/7?panel=review&scenario=5"
        assert "Boundary 01" in data["say_to_user"] and "Friction 01" in data["say_to_user"]
        assert "sign" in data["say_to_user"].lower()
        assert "get_scenario" in data["next_step"] and "unsigned_inputs" in data["next_step"]

    @respx.mock
    async def test_the_refusal_does_not_poll_or_wait_for_the_person(self, _server):
        detail = respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        _mock_project()
        respx.post(BUILD_URL).mock(return_value=httpx.Response(409, json=UNSIGNED_409))
        await _build(_server)
        assert detail.call_count == 1     # the pre-check read only: no poll loop

    @respx.mock
    async def test_the_outcome_is_keyed_on_error_code_not_on_the_409(self, _server):
        """NEGATIVE CONTROL: another 409 with an error_code is relayed as before, no review link."""
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        _mock_project()
        other = {"error_code": "COMPUTE_TARGET_UNAVAILABLE", "detail": "misconfigured"}
        respx.post(BUILD_URL).mock(return_value=httpx.Response(409, json=other))
        data = _tool_json(await _build(_server))
        assert data.get("outcome") != "unsigned_inputs"
        assert "panel=review" not in (data["ui_url"] or "")


class TestRunRelay:
    @respx.mock
    async def test_a_refused_run_returns_unsigned_inputs_with_the_review_link(self, _server):
        _mock_project()
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(409, json=UNSIGNED_409))
        data = _tool_json(await _call_as_caller(_server, "start_simulation", {"scenario_id": 5}))
        assert data["outcome"] == "unsigned_inputs" and data["error_code"] == "UNSIGNED_INPUTS"
        assert data["ui_url"] == "https://hydrata.example.com/catalogue/#/map/7?panel=review&scenario=5"
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
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(
            409, json={"error_code": "INVALID_TRANSITION", "detail": "Scenario has no runs."}))
        result = await _call_as_caller(_server, "start_simulation", {"scenario_id": 5})
        assert result.get("isError") is True


class TestGetScenarioCarriesUnsignedInputs:
    @respx.mock
    async def test_get_scenario_compact_record_carries_unsigned_inputs(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(
            return_value=httpx.Response(200, json=_scenario(unsigned_inputs=UNSIGNED)))
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        assert data["unsigned_inputs"] == UNSIGNED

    @respx.mock
    async def test_a_stranger_read_has_no_unsigned_inputs_key(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(
            return_value=httpx.Response(200, json={"id": 5, "name": "x", "latest_run": None}))
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        assert "unsigned_inputs" not in data          # withheld is not "nothing to sign"


class TestInflowRuleAndInstructions:
    async def test_attach_input_layer_and_the_instructions_carry_the_inflow_rule(self, _server):
        for text in (_server.attach_input_layer.__doc__, _server.mcp.instructions):
            assert "never invent" in text.lower() and "inflow" in text.lower()

    async def test_the_instructions_say_the_person_signs_off_and_the_agent_polls(self, _server):
        text = _server.mcp.instructions
        assert "UNSIGNED_INPUTS" in text and "signs them off" in text
        assert "get_scenario" in text and "unsigned_inputs" in text
