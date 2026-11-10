"""TASK-4103: a complete run's graded stability reaches the agent, with a say_to_user line to relay.

hydrata serves `stability` on GET runs/<id>/ and runs/<id>/status/ (Run.stability_report): class, onset,
location, severity, message and warnings (BOUNDARY_FED_VOLUME_GAIN). Run 1613 (40 m, 4 h, rain-on-grid,
all-Transmissive catchment edge) is the real case: about 3.69M m3 came in through the open edge.
"""

import json

import httpx
import pytest
import respx

from tests.helpers import BASE, _run

pytestmark = pytest.mark.asyncio

DETAIL = (
    "About 3,687,762 m3 of water came into the model through its open boundary (Transmissive) after the rain "
    "and inflows ended, from 2:00 into the run. Depths and flows after that include water that is not from your "
    "rain or inflows."
)
HINT = (
    "If this model is driven only by rain and inflow, make the catchment outline Reflective, with an open "
    "boundary only at the outlet, then run it again."
)
BOUNDARY_FED = {
    "class": "boundary_fed",
    "speed_class": "clip_artefact",
    "onset_sim_s": 7200.0,
    "boundary_tag": "Transmissive",
    "boundary_net_in_m3": 3687762.0,
    "location": None,
    "thresholds_provisional": True,
    "severity": "warning",
    "message": DETAIL,
    "warnings": [
        {
            "error_code": "BOUNDARY_FED_VOLUME_GAIN",
            "detail": DETAIL,
            "hint": HINT,
            "severity": "warning",
            "boundary_tag": "Transmissive",
            "volume_m3": 3687762,
        }
    ],
}
CLIP = {"class": "clip_artefact", "speed_class": "clip_artefact", "severity": None, "message": None, "warnings": []}


def _mock_run(stability, run_id=1613):
    respx.get(f"{BASE}/runs/{run_id}/").mock(
        return_value=httpx.Response(200, json=_run(run_id, "complete", stability=stability))
    )
    respx.get(f"{BASE}/runs/{run_id}/sww/").mock(return_value=httpx.Response(200, json={"state": "not_kept"}))
    respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1}))
    respx.get(f"{BASE}/runs/{run_id}/structure-results/").mock(return_value=httpx.Response(200, json=[]))


class TestGetRunStability:
    @respx.mock
    async def test_get_run_relays_stability(self, _server):
        _mock_run(BOUNDARY_FED)
        data = json.loads(await _server.get_run(run_id=1613))
        assert data["stability"]["class"] == "boundary_fed"
        assert data["stability"]["warnings"][0]["error_code"] == "BOUNDARY_FED_VOLUME_GAIN"
        line = data["say_to_user"]
        assert line.startswith("Run 1613: Warning: About 3,687,762 m3")
        assert "Transmissive" in line and "Reflective" in line

    @respx.mock
    async def test_get_run_says_nothing_for_a_clip_artefact(self, _server):
        _mock_run(CLIP)
        data = json.loads(await _server.get_run(run_id=1613))
        assert data["stability"]["class"] == "clip_artefact"
        assert "say_to_user" not in data

    @respx.mock
    async def test_get_run_information_line_for_a_speed_class(self, _server):
        message = "At 1:30 into the run the model computed implausibly fast flow for a moment; it settled on its own."
        _mock_run({"class": "local_transient", "severity": "info", "message": message, "warnings": []})
        data = json.loads(await _server.get_run(run_id=1613))
        assert data["say_to_user"] == f"Run 1613: Note: {message}"


class TestGetRunStatusStability:
    @respx.mock
    async def test_get_run_status_relays_stability_on_a_complete_run(self, _server):
        respx.get(f"{BASE}/runs/1613/status/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 1613,
                    "status": "complete",
                    "progress_pct": 100,
                    "eta_seconds": None,
                    "error_message": None,
                    "compute_backend": "batch",
                    "status_detail": None,
                    "stability": BOUNDARY_FED,
                },
            )
        )
        data = json.loads(await _server.get_run_status(run_id=1613))
        assert data["stability"]["class"] == "boundary_fed"
        line = data["say_to_user"]
        assert line.startswith("Run 1613 is complete.") and "Warning: About 3,687,762 m3" in line
        assert HINT in line

    @respx.mock
    async def test_get_run_status_complete_without_stability_is_unchanged(self, _server):
        respx.get(f"{BASE}/runs/9/status/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 9,
                    "status": "complete",
                    "progress_pct": 100,
                    "eta_seconds": None,
                    "error_message": None,
                    "compute_backend": "batch",
                    "status_detail": None,
                },
            )
        )
        line = json.loads(await _server.get_run_status(run_id=9))["say_to_user"]
        assert line == "Run 9 is complete. Ask me for the results, or open it in the map."
