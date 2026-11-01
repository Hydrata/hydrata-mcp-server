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


class _Clock:
    """A fake clock: sleeping advances time, so no test sleeps for real."""

    def __init__(self, server):
        self.t = 0.0
        self.server = server

    async def sleep(self, seconds):
        self.t += seconds

    def __enter__(self):
        h = __import__("hydrata_mcp._helpers", fromlist=["x"])
        self._h = h
        self._orig = (h.time.monotonic, h.asyncio.sleep)
        h.time.monotonic = lambda: self.t
        h.asyncio.sleep = self.sleep
        return self

    def __exit__(self, *exc):
        self._h.time.monotonic, self._h.asyncio.sleep = self._orig


def _sequence(*bodies):
    return [httpx.Response(200, json={"id": 9, "compute_backend": "batch", **b}) for b in bodies]


class TestWaitSeconds:
    @respx.mock
    async def test_wait_seconds_zero_reads_once(self, _server):
        route = respx.get(f"{BASE}/runs/9/status/").mock(return_value=_status(progress_pct=10))
        data = json.loads(await _server.get_run_status(run_id=9))
        assert route.call_count == 1 and "waited_seconds" not in data

    @respx.mock
    async def test_wait_seconds_returns_early_on_five_point_progress(self, _server):
        route = respx.get(f"{BASE}/runs/9/status/").mock(
            side_effect=_sequence(
                {"status": "computing", "progress_pct": 10},
                {"status": "computing", "progress_pct": 13},
                {"status": "computing", "progress_pct": 16},
                {"status": "computing", "progress_pct": 17},
            )
        )
        with _Clock(_server) as clock:
            data = json.loads(await _server.get_run_status(run_id=9, wait_seconds=180))
        assert route.call_count == 3
        assert data["progress_pct"] == 16 and data["waited_seconds"] == 10.0 and data["timed_out"] is False
        assert clock.t == 10.0

    @respx.mock
    async def test_wait_seconds_returns_early_on_phase_change(self, _server):
        respx.get(f"{BASE}/runs/9/status/").mock(
            side_effect=_sequence(
                {"status": "computing", "progress_pct": None},
                {"status": "computing", "progress_pct": 1},
            )
        )
        with _Clock(_server):
            data = json.loads(await _server.get_run_status(run_id=9, wait_seconds=60))
        assert data["phase"] == "computing" and data["waited_seconds"] == 5.0

    @respx.mock
    async def test_wait_seconds_returns_at_once_when_terminal(self, _server):
        route = respx.get(f"{BASE}/runs/9/status/").mock(return_value=_status(status="complete", progress_pct=100))
        with _Clock(_server):
            data = json.loads(await _server.get_run_status(run_id=9, wait_seconds=180))
        assert route.call_count == 1 and data["waited_seconds"] == 0.0 and "next_step" not in data

    @respx.mock
    async def test_wait_seconds_times_out_at_the_ceiling_with_next_step(self, _server):
        respx.get(f"{BASE}/runs/9/status/").mock(return_value=_status(progress_pct=20, eta_seconds=600))
        with _Clock(_server) as clock:
            data = json.loads(await _server.get_run_status(run_id=9, wait_seconds=9999))
        assert data["timed_out"] is True and data["waited_seconds"] == 180.0 and clock.t == 180.0
        assert "wait_seconds=180" in data["next_step"] and "10 minutes" in data["next_step"]
        assert "20%" in data["say_to_user"]
