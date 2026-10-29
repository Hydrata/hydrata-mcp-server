"""TASK-3816 (epic 3801 W2) + TASK-3844 (W3.5) — the MCP half of "Compare with
observations": read a run's comparison with the project's flood observations,
exclude / include an observation with a reason.

TASK-3844 (operator 18-10-26): the epic compares a run with observations; it
does not calibrate or validate the model. User-facing name "Compare with
observations"; the tool is get_observation_comparison (was get_calibration),
the routes are /runs/<id>/comparison/ (was /calibration/), and the
calibration / validation role (set_observation_role) is gone.

- get_observation_comparison: headline + summary + gauges + datum checks + one
  row per flood observation. A Gauge record's `modelled_series` (every saved
  frame) is elided to its point count unless include_series is asked for.
  `layer_id` + `fid` narrow the rows to one observation and add its history.
- set_observation_exclusion: an exclusion NEEDS the engineer's reason; the tool
  never invents one (refused before any request), the API keeps the log.

NEGATIVE CONTROLS: a design run answers 404 NO_COMPARISON, which the tool
reports as "this run has no comparison" (not an error to retry); a
non-member's plain 404 and an anonymous caller's 401 ARE errors; an exclusion
without a reason sends nothing.
"""

import copy
import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    BASE_MAP,
    CALLER_KEY,
    KEY_HEADERS,
    MCP_HEADERS,
    _asgi_client,
    _call_as_caller,
    _parse,
    _rpc,
    _tool_error_text,
    _tool_json,
    _tools_call,
    reload_server,
)

pytestmark = pytest.mark.asyncio

RUN = 1591
COMPARISON = f"{BASE}/runs/{RUN}/comparison/"
EXCLUSIONS = f"{COMPARISON}exclusions/"
HISTORY = f"{COMPARISON}history/"
COMPARISON_TOOLS = {"get_observation_comparison", "set_observation_exclusion", "set_comparison_tolerance"}
TOLERANCE = f"{BASE}/projects/42/comparison-tolerance/"


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _row(fid, name, **overrides):
    row = {
        "id": 800 + fid,
        "layer": 239,
        "layer_title": "Survey 1998",
        "fid": fid,
        "name": name,
        "subtype": "flood_mark",
        "subtype_label": "Flood mark",
        "value_kind": "stage",
        "bound": "exact",
        "sigma_m": 0.1,
        "uncertainty_grade": "H",
        "observed_start": None,
        "observed_end": None,
        "datum": {"layer_datum": "AHD", "offset_m": 0.0, "status": "known", "reason": None},
        "match": "matched",
        "observed_m": 4.32,
        "modelled_m": 4.12,
        "residual_m": -0.2,
        "scored": True,
        "not_scored_reason": None,
        "within": True,
        "sample": {"outcome": "wet", "in_mesh": True, "method": "centroid"},
        "time": None,
        "excluded": False,
        "exclusion": None,
        "computed_at": "2026-10-18T00:01:02.123+00:00",
    }
    row.update(overrides)
    return row


GAUGE = {
    "layer": 239,
    "fid": 49,
    "name": "Pioneer Rd",
    "excluded": False,
    "scored": True,
    "not_scored_reason": None,
    "n_recorded": 97,
    "recorded_peak_m": 4.32,
    "modelled_peak_m": 4.1,
    "peak_error_m": -0.22,
    "timing_error_s": 972.0,
    "nse": 0.81,
    "frame_interval_s": 864.0,
}
SERIES = {"time_s": [0.0, 864.0, 1728.0], "stage_m": [1.0, 2.5, 4.1]}
PAYLOAD = {
    "run": RUN,
    "clock_kind": "historic",
    "run_window": {"start": "1998-08-16T23:00:00Z", "end": "1998-08-17T23:00:00Z"},
    "tolerance_m": 0.3,
    "tolerance_presets_m": [0.15, 0.3, 0.4],
    # TASK-3845: the API sends the headline as numbers; MCP words it in English.
    "headline": {
        "n": 2,
        "within": 1,
        "within_pct": 50.0,
        "tolerance_m": 0.3,
        "bias_m": -0.4,
        "rmse_m": 0.45,
        "weighted_rmse_m": 0.44,
        "excluded": 1,
        "gauges": 1,
        "needs_date": 0,
    },
    "summary": {
        "n": 2,
        "within": 1,
        "within_pct": 50.0,
        "bias_m": -0.4,
        "rmse_m": 0.45,
        "weighted_rmse_m": 0.44,
        "mae_m": 0.4,
        "max_abs_m": 0.6,
        "tolerance_m": 0.3,
        "excluded": 1,
        "not_scored": {"no_model": 0},
        "gauges": 1,
    },
    "gauges": [GAUGE],
    "datum_checks": [
        {
            "layer": 239,
            "layer_title": "Survey 1998",
            "layer_datum": "AHD",
            "terrain_datum": "orthometric_ahd",
            "status": "match",
            "message": None,
        }
    ],
    "rows": [
        _row(17, "FM-01"),
        _row(18, "FM-02", residual_m=-0.6, within=False),
        _row(
            19,
            "FM-03",
            excluded=True,
            exclusion={"reason": "mark on a fence", "by": "dave", "at": "2026-10-18T00:12:00+00:00"},
        ),
        _row(
            49,
            "Pioneer Rd",
            subtype="gauge_record",
            subtype_label="Gauge record",
            within=None,
            gauge=GAUGE,
            modelled_series=SERIES,
        ),
    ],
}
HEADLINE = "2 observations · 1 within ±0.30 m (50%) · bias -0.40 m · RMSE 0.45 m · 1 excluded"
NO_COMPARISON = {
    "error_code": "NO_COMPARISON",
    "detail": "Compare with observations is available only for historic runs (every clocked "
    "forcing series recorded); this run is not one.",
}


def _mock_run(project=42, scenario=489):
    respx.get(f"{BASE}/runs/{RUN}/").mock(
        return_value=httpx.Response(
            200,
            json={"id": RUN, "project": project, "scenario": scenario, "status": "complete", "clock_kind": "historic"},
        )
    )
    respx.get(f"{BASE}/projects/{project}/").mock(
        return_value=httpx.Response(200, json={"id": project, "name": "p", "base_map": BASE_MAP})
    )


def _state(excluded=True, reason="mark on a fence, not the flood"):
    return {
        "run": RUN,
        "layer": 239,
        "fid": 17,
        "excluded": excluded,
        "reason": reason,
        "by": "dave",
        "at": "2026-10-18T00:12:00.000+00:00",
    }


class TestGetComparison:
    @respx.mock
    async def test_get_comparison_relays_headline_summary_and_every_row(self, _server):
        _mock_run()
        route = respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=PAYLOAD))
        data = _tool_json(await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN}))
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert "tolerance" not in route.calls[0].request.url.params  # the API's 0.3 m default
        assert data["has_comparison"] is True
        assert data["headline"] == HEADLINE  # English, built from the numbers
        assert data["headline_numbers"] == PAYLOAD["headline"]
        assert data["statistics"] == PAYLOAD["summary"] and data["gauges"] == PAYLOAD["gauges"]
        assert data["datum_checks"] == PAYLOAD["datum_checks"]
        assert [row["fid"] for row in data["rows"]] == [17, 18, 19, 49]  # excluded ones too
        assert data["rows"][2]["exclusion"]["reason"] == "mark on a fence"
        assert data["row_count"] == 4
        # hand-back: the run on its scenario in the map; the headline is what to say
        assert f"/catalogue/#/map/{BASE_MAP}" in data["ui_url"]
        assert f"run={RUN}" in data["ui_url"] and "scenario=489" in data["ui_url"]
        assert HEADLINE in data["say_to_user"]
        for word in ("pass", "fail", "footprint", "calibration target"):
            assert word not in data["say_to_user"].lower(), word

    @respx.mock
    async def test_a_terrain_with_no_recorded_datum_is_not_a_warning(self, _server):
        """TASK-3850: 'not recorded' is stated, never warned about; a real
        mismatch (both known, different) still is."""
        _mock_run()
        payload = copy.deepcopy(PAYLOAD)
        payload["datum_checks"] = [
            {
                "layer": 239,
                "layer_title": "Survey 1998",
                "layer_datum": "AHD",
                "terrain_datum": None,
                "status": "not_recorded",
                "warning": False,
                "message": None,
                "note": "Terrain datum not recorded - assumed the same as the observations.",
            }
        ]
        route = respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=payload))
        data = _tool_json(await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN}))
        assert "warning" not in data["say_to_user"].lower()
        assert "Terrain datum not recorded - assumed the same as the observations." in data["say_to_user"]
        payload["datum_checks"] = [
            {
                "layer": 239,
                "layer_title": "Survey 1998",
                "layer_datum": "EPSG:3855",
                "terrain_datum": {"value": "EPSG:5711", "label": "AHD height (EPSG:5711)", "source": "editor"},
                "status": "mismatch",
                "warning": True,
                "message": "This layer's levels are on EGM2008 height ...",
                "note": None,
            }
        ]
        route.mock(return_value=httpx.Response(200, json=payload))
        data = _tool_json(await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN}))
        assert "Datum warning: This layer's levels are on EGM2008 height" in data["say_to_user"]

    @respx.mock
    async def test_an_older_run_read_at_the_nearest_node_is_said(self, _server):
        """TASK-3843: rows of a run whose store predates the per-triangle centroid
        peak are read at the nearest node; the tool tells the user (an older run's
        corner value), and says nothing for a current run."""
        _mock_run()
        older = copy.deepcopy(PAYLOAD)
        older["sampling"] = {"centroid": 0, "nearest_node_fallback": 3, "nearest_node": 0}
        respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=older))
        data = _tool_json(await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN}))
        assert data["sampling"] == older["sampling"]
        assert "older run" in data["say_to_user"] and "nearest mesh node" in data["say_to_user"]
        assert "3 observations" in data["say_to_user"]

    @respx.mock
    async def test_negative_control_a_centroid_run_has_no_older_run_note(self, _server):
        _mock_run()
        current = copy.deepcopy(PAYLOAD)
        current["sampling"] = {"centroid": 4, "nearest_node_fallback": 0, "nearest_node": 0}
        respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=current))
        data = _tool_json(await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN}))
        assert "older run" not in data["say_to_user"]

    @respx.mock
    async def test_get_comparison_elides_gauge_series_unless_asked(self, _server):
        _mock_run()
        respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=copy.deepcopy(PAYLOAD)))
        data = _tool_json(await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN}))
        gauge_row = data["rows"][3]
        assert "modelled_series" not in gauge_row
        assert gauge_row["modelled_series_points"] == 3
        assert gauge_row["gauge"]["nse"] == 0.81  # the gauge figures stay
        data = _tool_json(
            await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN, "include_series": True})
        )
        assert data["rows"][3]["modelled_series"] == SERIES

    @respx.mock
    async def test_get_comparison_forwards_tolerance_and_relays_its_refusal(self, _server):
        _mock_run()
        route = respx.get(COMPARISON).mock(
            side_effect=[
                httpx.Response(200, json={**PAYLOAD, "tolerance_m": 0.15}),
                httpx.Response(
                    400,
                    json={
                        "error_code": "VALIDATION_ERROR",
                        "problems": {"tolerance": "must be a number of metres above 0 and at most 5"},
                    },
                ),
            ]
        )
        data = _tool_json(
            await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN, "tolerance": 0.15})
        )
        assert route.calls[0].request.url.params["tolerance"] == "0.15"
        assert data["tolerance_m"] == 0.15
        text = _tool_error_text(
            await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN, "tolerance": 9})
        )
        assert "tolerance" in text and "at most 5" in text

    @respx.mock
    async def test_get_comparison_one_observation_with_its_history(self, _server):
        _mock_run()
        respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=PAYLOAD))
        history = respx.get(HISTORY).mock(
            return_value=httpx.Response(
                200,
                json={
                    "run": RUN,
                    "layer": 239,
                    "fid": 19,
                    "history": [
                        {
                            "action": "exclude",
                            "reason": "mark on a fence",
                            "by": "dave",
                            "at": "2026-10-18T00:12:00+00:00",
                        }
                    ],
                },
            )
        )
        data = _tool_json(
            await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN, "layer_id": 239, "fid": 19})
        )
        assert history.calls[0].request.url.params["layer"] == "239"
        assert history.calls[0].request.url.params["fid"] == "19"
        assert [row["fid"] for row in data["rows"]] == [19] and data["row_count"] == 1
        assert data["history"][0]["action"] == "exclude"
        assert "FM-03: residual -0.2 m" in data["say_to_user"] and "(excluded)" in data["say_to_user"]
        assert data["statistics"] == PAYLOAD["summary"]  # the run's figures, not one row's
        assert data["summary"]["run_id"] == RUN  # the hand-back's own summary beside it

    @respx.mock
    async def test_get_comparison_unknown_observation_is_refused(self, _server):
        _mock_run()
        respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=PAYLOAD))
        text = _tool_error_text(
            await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN, "layer_id": 239, "fid": 999})
        )
        assert "fid 999" in text and "no row" in text and "alibration" not in text
        text = _tool_error_text(
            await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN, "fid": 19})
        )
        assert "layer_id and fid" in text

    @respx.mock
    async def test_get_comparison_unscored_observation_says_why(self, _server):
        _mock_run()
        payload = copy.deepcopy(PAYLOAD)
        payload["rows"][1].update(
            scored=False, residual_m=None, modelled_m=None, within=None, not_scored_reason="no_model"
        )
        respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=payload))
        respx.get(HISTORY).mock(return_value=httpx.Response(200, json={"history": []}))
        data = _tool_json(
            await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN, "layer_id": 239, "fid": 18})
        )
        assert "FM-02: not scored (no_model)" in data["say_to_user"]
        assert "None" not in data["say_to_user"]


class TestHeadlineSentence:
    """TASK-3845: hydrata sends the headline's numbers (the map words them per
    locale); MCP keeps the English sentence beside them."""

    async def test_sentence_from_numbers(self, _server):
        h = dict(PAYLOAD["headline"])
        assert _server._headline_sentence(h) == HEADLINE
        h.update(n=1, within=1, within_pct=100.0, bias_m=0.123, rmse_m=0.123, excluded=0, needs_date=2)
        assert _server._headline_sentence(h) == (
            "1 observation · 1 within ±0.30 m (100%) · bias +0.12 m · RMSE 0.12 m · 0 excluded · 2 need a date"
        )

    async def test_nothing_scored_has_no_figures(self, _server):
        h = {
            "n": 0,
            "within": 0,
            "within_pct": None,
            "tolerance_m": 0.15,
            "bias_m": None,
            "rmse_m": None,
            "weighted_rmse_m": None,
            "excluded": 3,
            "gauges": 0,
            "needs_date": 1,
        }
        assert _server._headline_sentence(h) == "0 observations · 0 within ±0.15 m · 3 excluded · 1 needs a date"

    async def test_dry_points_get_their_own_part(self, _server):
        """TASK-3983 (epic 3927): since TASK-3981 dry points are out of n / bias / RMSE
        and come as `dry_points`; the sentence words them as the map does."""
        h = dict(PAYLOAD["headline"])
        plain = _server._headline_sentence(h)
        h["dry_points"] = {"n": 1, "wet": 1, "worst_name": "DP1", "worst_depth_m": 0.86}
        assert _server._headline_sentence(h) == plain + " · Dry points: 1 of 1 wet in the model (DP1 0.86 m)"
        h["dry_points"] = {"n": 2, "wet": 0, "worst_name": None, "worst_depth_m": None}
        assert _server._headline_sentence(h) == plain + " · Dry points: 0 of 2 wet in the model"
        h["dry_points"] = {"n": 1, "wet": 1, "worst_name": None, "worst_depth_m": 0.4}
        assert _server._headline_sentence(h).endswith("(deepest 0.40 m)")

    async def test_an_older_server_sentence_passes_through(self, _server):
        assert _server._headline_sentence("2 observations · ...") == "2 observations · ..."
        assert _server._headline_sentence(None) is None


class TestNegativeControls:
    @respx.mock
    async def test_get_comparison_design_run_has_none_and_is_not_an_error(self, _server):
        _mock_run()
        respx.get(COMPARISON).mock(return_value=httpx.Response(404, json=NO_COMPARISON))
        data = _tool_json(await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN}))
        assert data["has_comparison"] is False and data["error_code"] == "NO_COMPARISON"
        assert "rows" not in data and "headline" not in data
        assert "not a historic run" in data["say_to_user"]
        assert "retry" not in data["say_to_user"].lower()

    @respx.mock
    async def test_get_comparison_non_member_404_is_an_error(self, _server):
        respx.get(COMPARISON).mock(return_value=httpx.Response(404, json={"detail": "Not found."}))
        text = _tool_error_text(await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN}))
        assert "404" in text

    @respx.mock
    async def test_get_comparison_anonymous_is_refused(self, _server):
        route = respx.get(COMPARISON).mock(
            return_value=httpx.Response(401, json={"detail": "Authentication credentials were not provided."})
        )
        async with _asgi_client(_server) as c:  # no Authorization header at all
            resp = await c.post(
                "/", headers=MCP_HEADERS, json=_tools_call("get_observation_comparison", {"run_id": RUN})
            )
        # the server holds no identity: an anonymous tools/call never reaches the API
        assert resp.status_code == 401
        assert route.call_count == 0
        # a credential the API rejects is an error too, never an empty comparison
        text = _tool_error_text(await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN}))
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert "401" in text

    @respx.mock
    async def test_set_observation_exclusion_without_reason_sends_nothing(self, _server):
        route = respx.post(EXCLUSIONS).mock(return_value=httpx.Response(201, json=_state()))
        for reason in ("", "   "):
            text = _tool_error_text(
                await _call_as_caller(
                    _server,
                    "set_observation_exclusion",
                    {"run_id": RUN, "layer_id": 239, "fid": 17, "excluded": True, "reason": reason},
                )
            )
            assert "reason" in text and "ask" in text.lower()
        assert route.call_count == 0


class TestExclusion:
    @respx.mock
    async def test_set_observation_exclusion_posts_the_reason_and_reports_the_new_headline(self, _server):
        _mock_run()
        route = respx.post(EXCLUSIONS).mock(return_value=httpx.Response(201, json=_state()))
        respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=PAYLOAD))
        data = _tool_json(
            await _call_as_caller(
                _server,
                "set_observation_exclusion",
                {"run_id": RUN, "layer_id": 239, "fid": 17, "reason": "mark on a fence, not the flood"},
            )
        )
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert json.loads(route.calls[0].request.content) == {
            "layer": 239,
            "fid": 17,
            "excluded": True,
            "reason": "mark on a fence, not the flood",
        }
        assert data["excluded"] is True and data["by"] == "dave" and data["http_status"] == 201
        assert data["headline_after"] == HEADLINE
        assert "excluded" in data["say_to_user"] and "mark on a fence" in data["say_to_user"]
        assert f"run={RUN}" in data["ui_url"]

    @respx.mock
    async def test_set_observation_exclusion_includes_again_without_a_reason(self, _server):
        _mock_run()
        route = respx.post(EXCLUSIONS).mock(return_value=httpx.Response(201, json=_state(excluded=False, reason=None)))
        respx.get(COMPARISON).mock(return_value=httpx.Response(200, json=PAYLOAD))
        data = _tool_json(
            await _call_as_caller(
                _server, "set_observation_exclusion", {"run_id": RUN, "layer_id": 239, "fid": 17, "excluded": False}
            )
        )
        assert json.loads(route.calls[0].request.content) == {"layer": 239, "fid": 17, "excluded": False, "reason": ""}
        assert data["excluded"] is False and "included again" in data["say_to_user"]

    @respx.mock
    async def test_set_observation_exclusion_headline_failure_keeps_the_change(self, _server):
        _mock_run()
        respx.post(EXCLUSIONS).mock(return_value=httpx.Response(201, json=_state()))
        respx.get(COMPARISON).mock(return_value=httpx.Response(503))
        data = _tool_json(
            await _call_as_caller(
                _server,
                "set_observation_exclusion",
                {"run_id": RUN, "layer_id": 239, "fid": 17, "reason": "mark on a fence, not the flood"},
            )
        )
        assert data["excluded"] is True and data["headline_after"] is None
        assert "headline_error" in data

    @respx.mock
    async def test_set_observation_exclusion_refusals_are_errors(self, _server):
        respx.post(EXCLUSIONS).mock(
            side_effect=[
                httpx.Response(403, json={"detail": "You do not have permission to perform this action."}),
                httpx.Response(404, json=NO_COMPARISON),
                httpx.Response(404, json={"detail": "This observation has no row in this run's comparison."}),
            ]
        )
        args = {"run_id": RUN, "layer_id": 239, "fid": 17, "reason": "mark on a fence"}
        assert "403" in _tool_error_text(await _call_as_caller(_server, "set_observation_exclusion", args))
        text = _tool_error_text(await _call_as_caller(_server, "set_observation_exclusion", args))
        assert "no comparison" in text and "not a historic run" in text
        text = _tool_error_text(await _call_as_caller(_server, "set_observation_exclusion", args))
        assert "has no row" in text


class TestSavedTolerance:
    """TASK-3848: an Editor saves the project's default tolerance; the read
    uses it unless the agent passes `tolerance` (which saves nothing)."""

    @respx.mock
    async def test_set_comparison_tolerance_saves_it(self, _server):
        _mock_run()
        route = respx.patch(TOLERANCE).mock(return_value=httpx.Response(200, json={"project": 42, "tolerance_m": 0.4}))
        data = _tool_json(
            await _call_as_caller(_server, "set_comparison_tolerance", {"project_id": 42, "tolerance_m": 0.4})
        )
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert json.loads(route.calls[0].request.content) == {"tolerance_m": 0.4}
        assert data["tolerance_m"] == 0.4 and "±0.40 m" in data["say_to_user"]
        assert f"/catalogue/#/map/{BASE_MAP}" in data["ui_url"]

    @respx.mock
    async def test_negative_control_a_viewer_cannot_save(self, _server):
        respx.patch(TOLERANCE).mock(
            return_value=httpx.Response(403, json={"detail": "Editor role or higher required."})
        )
        text = _tool_error_text(
            await _call_as_caller(_server, "set_comparison_tolerance", {"project_id": 42, "tolerance_m": 0.4})
        )
        assert "403" in text

    @respx.mock
    async def test_a_tolerance_on_the_read_is_forwarded_and_not_saved(self, _server):
        _mock_run()
        saved = respx.patch(TOLERANCE).mock(return_value=httpx.Response(200, json={}))
        respx.get(COMPARISON).mock(
            return_value=httpx.Response(200, json={**PAYLOAD, "tolerance_m": 0.15, "default_tolerance_m": 0.4})
        )
        data = _tool_json(
            await _call_as_caller(_server, "get_observation_comparison", {"run_id": RUN, "tolerance": 0.15})
        )
        assert (data["tolerance_m"], data["default_tolerance_m"]) == (0.15, 0.4)
        assert saved.call_count == 0


class TestComparisonToolList:
    async def test_comparison_tools_are_listed_with_their_hints(self, _server):
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        assert COMPARISON_TOOLS <= set(tools)
        assert tools["get_observation_comparison"]["annotations"]["readOnlyHint"] is True
        for name in ("set_observation_exclusion", "set_comparison_tolerance"):
            ann = tools[name]["annotations"]
            assert ann["readOnlyHint"] is False and ann["destructiveHint"] is False, name
            assert ann["idempotentHint"] is True, name

    async def test_no_calibration_wording_and_no_role_tool(self, _server):
        """TASK-3844: no tool is named or described as Calibration, and the
        calibration / validation role tool is gone."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        assert "get_calibration" not in tools and "set_observation_role" not in tools
        for name, tool in tools.items():
            text = json.dumps([name, tool.get("description"), tool.get("inputSchema"), tool.get("annotations")]).lower()
            assert "calibrat" not in text, name
        assert "calibrat" not in (_server.mcp.instructions or "").lower()
