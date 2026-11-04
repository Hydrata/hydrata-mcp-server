"""TASK-4032: get_flow_hydrograph posts the line, relays the peak, and never tracebacks on a 404."""

import json

import httpx
import pytest
import respx

from tests.helpers import BASE

pytestmark = pytest.mark.asyncio

LINE = [[149.12, -35.28], [149.13, -35.28]]
# TASK-4187: the backend's plain-word peak caveat and outflow note (hydrata gn_anuga.flow_hydrograph)
NEW_PEAK = (
    "Results are saved every 60 s, so the true peak may fall up to 30 s either side of the time shown "
    "and may be slightly higher."
)
OLD_PEAK = "the peak is resolved to the nearest frame (+-frame_spacing_s/2); the true peak may fall between frames"
OUTFLOW_NOTE = (
    "the water that left through the model's open edges is rain + inflow minus the water left in the model; "
    "it is not measured at the edge"
)
DRY = "the line touches no wet triangle: the flow is zero"


def _answer(**kw):
    body = {
        "run_id": 9,
        "line": LINE,
        "time_s": [0.0, 60.0, 120.0],
        "q_m3s": [0.0, 12.3, 4.0],
        "peak_m3s": 12.3,
        "peak_time_s": 5700.0,
        "volume_m3": 900.0,
        "sign_convention": "Positive flow crosses the line from its LEFT bank to its RIGHT bank.",
        "frame_spacing_s": 60.0,
        "warnings": [NEW_PEAK],
        "mass_balance": {
            "rain_in_m3": 1000.0,
            "inflow_in_m3": 200.0,
            "left_in_model_m3": 700.0,
            "out_by_difference_m3": 500.0,
            "note": OUTFLOW_NOTE,
        },
    }
    body.update(kw)
    return body


class TestFlowHydrograph:
    @respx.mock
    async def test_posts_the_line_and_returns_the_numbers(self, _server):
        route = respx.post(f"{BASE}/runs/9/flow-hydrograph/").mock(return_value=httpx.Response(200, json=_answer()))
        data = json.loads(await _server.get_flow_hydrograph(run_id=9, line=LINE))
        assert json.loads(route.calls[0].request.content) == {"line": LINE}
        assert data["peak_m3s"] == 12.3 and data["q_m3s"] == [0.0, 12.3, 4.0]

    @respx.mock
    async def test_say_to_user_states_peak_time_volume_sign_and_mass_balance(self, _server):
        respx.post(f"{BASE}/runs/9/flow-hydrograph/").mock(return_value=httpx.Response(200, json=_answer()))
        say = json.loads(await _server.get_flow_hydrograph(run_id=9, line=LINE))["say_to_user"]
        assert "12.30 m3/s" in say and "1:35" in say and "900 m3" in say
        assert "LEFT bank to its RIGHT bank" in say
        assert "1,000 m3 of rain" in say and "700 m3 is left in the model" in say and "500 m3 left" in say

    @respx.mock
    async def test_say_to_user_drops_the_new_peak_caveat_from_the_note(self, _server):
        respx.post(f"{BASE}/runs/9/flow-hydrograph/").mock(
            return_value=httpx.Response(200, json=_answer(warnings=[NEW_PEAK, DRY]))
        )
        say = json.loads(await _server.get_flow_hydrograph(run_id=9, line=LINE))["say_to_user"]
        note = say.split("Note: ")[1]
        assert DRY in note and "Results are saved" not in say

    @respx.mock
    async def test_say_to_user_drops_the_old_peak_caveat_from_the_note(self, _server):
        """A hydrata that still sends the old caveat (MCP shipped first) relays no duplicate either."""
        respx.post(f"{BASE}/runs/9/flow-hydrograph/").mock(
            return_value=httpx.Response(200, json=_answer(warnings=[OLD_PEAK, DRY]))
        )
        say = json.loads(await _server.get_flow_hydrograph(run_id=9, line=LINE))["say_to_user"]
        note = say.split("Note: ")[1]
        assert DRY in note and "nearest frame (+-" not in say and "frame_spacing_s" not in say

    @respx.mock
    async def test_say_to_user_has_no_note_when_the_peak_caveat_is_the_only_warning(self, _server):
        respx.post(f"{BASE}/runs/9/flow-hydrograph/").mock(return_value=httpx.Response(200, json=_answer()))
        say = json.loads(await _server.get_flow_hydrograph(run_id=9, line=LINE))["say_to_user"]
        assert "Note:" not in say

    @respx.mock
    async def test_say_to_user_names_the_outflow_in_plain_words(self, _server):
        respx.post(f"{BASE}/runs/9/flow-hydrograph/").mock(return_value=httpx.Response(200, json=_answer()))
        say = json.loads(await _server.get_flow_hydrograph(run_id=9, line=LINE))["say_to_user"]
        assert "500 m3 left through the model's open edges" in say
        assert "rain + inflow minus the water left in the model; it is not measured at the edge" in say
        assert "boundary flux" not in say and "by difference" not in say

    @respx.mock
    async def test_unreadable_rain_still_gives_a_mass_balance_sentence(self, _server):
        mb = {"rain_in_m3": None, "inflow_in_m3": None, "left_in_model_m3": 700.0, "out_by_difference_m3": None}
        respx.post(f"{BASE}/runs/9/flow-hydrograph/").mock(
            return_value=httpx.Response(200, json=_answer(mass_balance=mb))
        )
        say = json.loads(await _server.get_flow_hydrograph(run_id=9, line=LINE))["say_to_user"]
        assert "700 m3 is left in the model" in say and "could not be read" in say

    @respx.mock
    async def test_404_is_a_no_playback_store_outcome_not_a_traceback(self, _server):
        respx.post(f"{BASE}/runs/9/flow-hydrograph/").mock(
            return_value=httpx.Response(404, json={"error_code": "NO_PLAYBACK_STORE", "error": "x"})
        )
        data = json.loads(await _server.get_flow_hydrograph(run_id=9, line=LINE))
        assert data["outcome"] == "no_playback_store"
        assert "no playback store" in data["say_to_user"]

    @respx.mock
    async def test_400_is_a_bad_line_outcome_with_the_reason(self, _server):
        respx.post(f"{BASE}/runs/9/flow-hydrograph/").mock(
            return_value=httpx.Response(400, json={"error_code": "BAD_LINE", "error": "line has zero length"})
        )
        data = json.loads(await _server.get_flow_hydrograph(run_id=9, line=LINE))
        assert data["outcome"] == "bad_line" and "zero length" in data["say_to_user"]

    async def test_the_tool_is_registered_read_only(self, _server):
        tools = {t.name: t for t in await _server.mcp.list_tools()}
        ann = tools["get_flow_hydrograph"].annotations
        assert ann.read_only_hint is True and ann.destructive_hint is False
