"""Run tools: start_simulation (incl. consent), run status, get, cancel, retry and list."""

import inspect
import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    SCENARIO_URL,
    _assert_hand_back,
    _build,
    _call_as_caller,
    _mock_boundary,
    _mock_build,
    _mock_project,
    _presigned_run,
    _run,
    _scenario,
    _terrain,
    _tool_json,
)

pytestmark = pytest.mark.asyncio


class TestStartSimulation:
    @respx.mock
    async def test_returns_run_with_status(self, _server):
        respx.post(f"{BASE}/scenarios/5/run/").mock(
            return_value=httpx.Response(202, json={"id": 10, "status": "queued"})
        )
        result = await _server.start_simulation(scenario_id=5)
        data = json.loads(result)
        assert data["http_status"] == 202
        assert data["status"] == "queued"

    @respx.mock
    async def test_non_dict_body(self, _server):
        """Non-dict body is wrapped safely."""
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(200, json="accepted"))
        result = await _server.start_simulation(scenario_id=5)
        data = json.loads(result)
        assert data["response"] == "accepted"
        assert data["http_status"] == 200


class TestGetRunStatus:
    @respx.mock
    async def test_returns_status(self, _server):
        respx.get(f"{BASE}/runs/10/status/").mock(
            return_value=httpx.Response(200, json={"id": 10, "status": "computing", "progress_pct": 45})
        )
        result = await _server.get_run_status(run_id=10)
        assert json.loads(result)["progress_pct"] == 45


class TestGetRun:
    @respx.mock
    async def test_returns_full_run(self, _server):
        run = {"id": 10, "status": "complete", "duration": 120.5}
        respx.get(f"{BASE}/runs/10/").mock(return_value=httpx.Response(200, json=run))
        result = await _server.get_run(run_id=10)
        assert json.loads(result)["duration"] == 120.5


class TestCancelRun:
    @respx.mock
    async def test_cancel(self, _server):
        respx.post(f"{BASE}/runs/10/cancel/").mock(
            return_value=httpx.Response(200, json={"id": 10, "status": "cancelled"})
        )
        result = await _server.cancel_run(run_id=10)
        data = json.loads(result)
        assert data["status"] == "cancelled"
        assert data["http_status"] == 200

    @respx.mock
    async def test_non_dict_body(self, _server):
        respx.post(f"{BASE}/runs/10/cancel/").mock(return_value=httpx.Response(200, json="ok"))
        result = await _server.cancel_run(run_id=10)
        data = json.loads(result)
        assert data["response"] == "ok"


class TestRetryRun:
    @respx.mock
    async def test_retry(self, _server):
        respx.post(f"{BASE}/runs/10/retry/").mock(
            return_value=httpx.Response(200, json={"id": 10, "status": "created"})
        )
        result = await _server.retry_run(run_id=10)
        data = json.loads(result)
        assert data["status"] == "created"
        assert data["http_status"] == 200

    @respx.mock
    async def test_non_dict_body(self, _server):
        respx.post(f"{BASE}/runs/10/retry/").mock(return_value=httpx.Response(200, json="retrying"))
        result = await _server.retry_run(run_id=10)
        data = json.loads(result)
        assert data["response"] == "retrying"


class TestListRuns:
    @respx.mock
    async def test_list_all(self, _server):
        respx.get(f"{BASE}/projects/1/runs/").mock(
            return_value=httpx.Response(200, json={"count": 2, "results": [{"id": 1}, {"id": 2}]})
        )
        result = await _server.list_runs(project_id=1)
        assert json.loads(result)["count"] == 2

    @respx.mock
    async def test_status_filter(self, _server):
        route = respx.get(f"{BASE}/projects/1/runs/").mock(
            return_value=httpx.Response(200, json={"count": 0, "results": []})
        )
        await _server.list_runs(project_id=1, status_filter="error")
        assert route.calls[0].request.url.params["status"] == "error"


class TestPhase:
    @pytest.mark.parametrize(
        "status,progress,phase",
        [
            ("computing", None, "preparing_inputs"),
            ("computing", 12, "computing"),
            ("queued", None, "queued"),
            ("complete", 100, "complete"),
            (None, None, "unknown"),
        ],
    )
    @respx.mock
    async def test_phase_get_run_status_is_never_empty(self, _server, status, progress, phase):
        respx.get(f"{BASE}/runs/9/status/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 9,
                    "status": status,
                    "progress_pct": progress,
                    "eta_seconds": None,
                    "error_message": None,
                    "compute_backend": "batch",
                },
            )
        )
        data = json.loads(await _server.get_run_status(run_id=9))
        assert data["phase"] == phase
        assert data["progress_pct"] == progress  # upstream fields untouched

    @pytest.mark.parametrize("status", ["creating", "styling", "ready", "error"])
    @respx.mock
    async def test_phase_get_terrain_is_the_import_stage(self, _server, status):
        respx.get(f"{BASE}/projects/42/terrain/110540/").mock(
            return_value=httpx.Response(200, json=_terrain(110540, status))
        )
        data = json.loads(await _server.get_terrain(project_id=42, terrain_id=110540, timeout_seconds=0))
        assert data["phase"] == status

    @respx.mock
    async def test_phase_get_terrain_not_found_and_null_status_never_empty(self, _server):
        respx.get(f"{BASE}/projects/42/terrain/").mock(return_value=httpx.Response(200, json=[]))
        data = json.loads(await _server.get_terrain(project_id=42, timeout_seconds=0))
        assert data["phase"] == "not_found"
        respx.get(f"{BASE}/projects/42/terrain/110541/").mock(
            return_value=httpx.Response(200, json=_terrain(110541, None))
        )
        data = json.loads(await _server.get_terrain(project_id=42, terrain_id=110541, timeout_seconds=0))
        assert data["phase"] == "unknown"


# ---------------------------------------------------------------------------
# TASK-3214 (W2a, epic 3200, D27) — run consent. Hydrata answers an agent's
# dispatch over its key's monthly spend limit (or an unpriceable one) with
# 409 CONSENT_REQUIRED + a consent link. start_simulation relays the code and
# the link as a NON-error result and polls the run for the person's click.
# The build 100k-triangle confirm stays: it protects the web box, not the
# budget. The stored proof selects on `-k consent`: every name carries it.
# ---------------------------------------------------------------------------
CONSENT_409 = {
    "error_code": "CONSENT_REQUIRED",
    "state": "consent_required",
    "detail": "This run is quoted at $10.00; this agent key has used $45.00 of its $50.00 monthly spend limit.",
    "consent_path": "/agents/consent/502/",
    "consent_url": "http://127.0.0.1:8081/agents/consent/502/",
    "run_id": 502,
    "scenario_id": 5,
    "quoted_cost_usd": "10.00",
    "mesh_triangle_count": 19812,
    "compute_target": "batch-gpu-l40s",
    "monthly_cap_usd": "50.00",
    "month_to_date_usd": "45.00",
}


class TestStartSimulationConsent:
    @respx.mock
    async def test_consent_required_is_relayed_with_the_link_on_the_ui_origin_not_an_error(self, _server):
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(409, json=CONSENT_409))
        status = respx.get(f"{BASE}/runs/502/status/").mock(
            return_value=httpx.Response(200, json={"id": 502, "status": "built"})
        )
        data = _tool_json(await _call_as_caller(_server, "start_simulation", {"scenario_id": 5}))
        assert data["outcome"] == "consent_required"
        assert data["error_code"] == "CONSENT_REQUIRED" and data["http_status"] == 409
        # the human opens the UI origin (prod's API URL is the loopback listener)
        assert data["consent_url"] == "https://hydrata.example.com/agents/consent/502/"
        assert data["quoted_cost_usd"] == "10.00" and data["run_id"] == 502
        assert data["consent_url"] in data["say_to_user"] and "$10.00" in data["say_to_user"]
        assert "start_simulation" in data["next_step"] and "get_run_status" in data["next_step"]
        assert status.call_count == 1  # default: one read, the link comes back at once

    @respx.mock
    async def test_consent_polls_until_the_person_presses_the_button(self, _server):
        _mock_project()
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(409, json=CONSENT_409))
        respx.get(f"{BASE}/runs/502/status/").mock(
            side_effect=[
                httpx.Response(200, json={"id": 502, "status": "built"}),
                httpx.Response(200, json={"id": 502, "status": "built"}),
                httpx.Response(200, json={"id": 502, "status": "queued"}),
            ]
        )
        respx.get(f"{BASE}/runs/502/").mock(return_value=httpx.Response(200, json=_run(502, "queued")))
        data = _tool_json(
            await _call_as_caller(
                _server,
                "start_simulation",
                {"scenario_id": 5, "consent_timeout_seconds": 30, "poll_interval_seconds": 0},
            )
        )
        assert data["outcome"] == "consented"
        assert data["status"] == "queued" and data["run_id"] == 502 and data["polls"] == 3
        _assert_hand_back(data, run=502)
        assert "consent" in data["say_to_user"]

    @respx.mock
    async def test_consent_timeout_returns_the_link_and_does_not_redispatch(self, _server):
        dispatch = respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(409, json=CONSENT_409))
        respx.get(f"{BASE}/runs/502/status/").mock(
            return_value=httpx.Response(200, json={"id": 502, "status": "built"})
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "start_simulation",
                {"scenario_id": 5, "consent_timeout_seconds": 1, "poll_interval_seconds": 0.2},
            )
        )
        assert data["outcome"] == "consent_required" and data["timed_out"] is True
        assert dispatch.call_count == 1

    @respx.mock
    async def test_consent_for_an_unpriceable_run_says_so(self, _server):
        body = dict(CONSENT_409, state="pending_human_consent", quoted_cost_usd=None)
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(409, json=body))
        respx.get(f"{BASE}/runs/502/status/").mock(
            return_value=httpx.Response(200, json={"id": 502, "status": "built"})
        )
        data = _tool_json(
            await _call_as_caller(_server, "start_simulation", {"scenario_id": 5, "consent_timeout_seconds": 0})
        )
        assert data["state"] == "pending_human_consent"
        assert "could not price" in data["say_to_user"]

    @respx.mock
    async def test_consent_other_4xx_still_raise_with_the_api_body(self, _server):
        from hydrata_mcp.client import HydrataAPIError

        respx.post(f"{BASE}/scenarios/5/run/").mock(
            return_value=httpx.Response(
                409, json={"error_code": "INVALID_TRANSITION", "detail": "Cannot transition from queued"}
            )
        )
        with pytest.raises(HydrataAPIError, match="409.*INVALID_TRANSITION"):
            await _server.start_simulation(scenario_id=5)

    @respx.mock
    async def test_consent_does_not_remove_the_build_100k_confirm(self, _server):
        """AC6: the build_scenario confirm above 100,000 triangles still refuses without confirm=true."""
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(estimate=255_412)))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called and data["outcome"] == "refused"
        assert "confirm=true" in data["reason"]

    async def test_consent_no_in_chat_consent_path_exists(self, _server):
        """D27 replaces in-chat (MRTR) consent: no tool takes a confirm/consent flag for a RUN."""

        params = inspect.signature(_server.start_simulation).parameters
        assert not {"confirm", "consent", "unattended"} & set(params)
        source = _server.module_source()
        assert "HYDRATA_MCP_UNATTENDED" not in source and "elicit" not in source


class TestGetRunSwwDownload:
    """TASK-4062 — get_run reports the time series as {state, size_bytes, ui_url}, never a link."""

    @respx.mock
    async def test_sww_download_state_size_and_ui_url_without_a_link(self, _server):
        run = _presigned_run(67181, "complete", project=42, scenario=7)
        respx.get(f"{BASE}/runs/67181/").mock(return_value=httpx.Response(200, json=run))
        respx.get(f"{BASE}/runs/67181/sww/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "state": "archived",
                    "size_bytes": 7_000_000,
                    "url": "https://b.s3.amazonaws.com/k.sww?X-Amz-Signature=zz",
                },
            )
        )
        respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1525}))
        result = await _server.get_run(run_id=67181)
        assert "X-Amz" not in result
        sww = json.loads(result)["sww"]
        assert set(sww) == {"state", "size_bytes", "ui_url"}
        assert sww["state"] == "archived" and sww["size_bytes"] == 7_000_000
        assert "/catalogue/#/map/1525" in sww["ui_url"] and "panel=results" in sww["ui_url"]

    @respx.mock
    async def test_sww_download_lookup_failure_does_not_fail_get_run(self, _server):
        run = _presigned_run(67181, "complete", project=42, scenario=7)
        respx.get(f"{BASE}/runs/67181/").mock(return_value=httpx.Response(200, json=run))
        respx.get(f"{BASE}/runs/67181/sww/").mock(return_value=httpx.Response(502, json={"detail": "x"}))
        data = json.loads(await _server.get_run(run_id=67181))
        assert data["id"] == 67181
        assert data["sww"]["state"] is None and "error" in data["sww"]
