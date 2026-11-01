"""TASK-4098 / TASK-4114: get_run_status says what to relay, and can wait."""

import json

import httpx
import pytest
import respx

from tests.helpers import BASE

pytestmark = pytest.mark.asyncio


def _status(**kw):
    base = {
        "id": 9,
        "status": "computing",
        "progress_pct": None,
        "eta_seconds": None,
        "error_message": None,
        "compute_backend": "batch",
        "status_detail": None,
    }
    base.update(kw)
    return httpx.Response(200, json=base)


class TestProgressLine:
    @respx.mock
    async def test_progress_line_has_stage_percent_and_eta(self, _server):
        respx.get(f"{BASE}/runs/9/status/").mock(return_value=_status(progress_pct=42.4, eta_seconds=780))
        data = json.loads(await _server.get_run_status(run_id=9))
        line = data["say_to_user"]
        assert "computing" in line and "42%" in line and "about 13 min" in line

    @respx.mock
    async def test_progress_line_before_first_progress_is_not_a_stall(self, _server):
        respx.get(f"{BASE}/runs/9/status/").mock(return_value=_status())
        line = json.loads(await _server.get_run_status(run_id=9))["say_to_user"]
        assert "preparing its inputs" in line and "not a stall" in line

    @respx.mock
    async def test_progress_line_without_eta_says_so(self, _server):
        respx.get(f"{BASE}/runs/9/status/").mock(return_value=_status(progress_pct=5))
        line = json.loads(await _server.get_run_status(run_id=9))["say_to_user"]
        assert "5%" in line and "not yet estimated" in line

    @pytest.mark.parametrize(
        "status,expect",
        [
            ("complete", "is complete"),
            ("error", "failed: boom"),
            ("cancelled", "was cancelled"),
            ("queued", "is queued"),
        ],
    )
    @respx.mock
    async def test_progress_line_terminal_and_queued(self, _server, status, expect):
        respx.get(f"{BASE}/runs/9/status/").mock(
            return_value=_status(
                status=status,
                progress_pct=100 if status == "complete" else None,
                error_message="boom" if status == "error" else None,
            )
        )
        line = json.loads(await _server.get_run_status(run_id=9))["say_to_user"]
        assert expect in line

    @respx.mock
    async def test_progress_line_prefers_server_phase_fields_untouched(self, _server):
        respx.get(f"{BASE}/runs/9/status/").mock(
            return_value=_status(progress_pct=50, eta_seconds=30, status_detail="Writing frames")
        )
        data = json.loads(await _server.get_run_status(run_id=9))
        assert data["status_detail"] == "Writing frames"
        assert "(Writing frames)" in data["say_to_user"]
        assert "under a minute" in data["say_to_user"]
