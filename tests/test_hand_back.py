"""The ui_url / summary / say_to_user hand-back every writing tool returns, and its refusals."""

import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    BASE_MAP,
    BOUNDARY_ALTERNATE,
    BOUNDARY_URL,
    EXEC_ID,
    EXEC_STATUS_URL,
    ORIGIN,
    ROW_DATA,
    SCENARIO_URL,
    STAGING_KEY,
    _assert_hand_back,
    _build,
    _dataset,
    _detail_to_built,
    _exec,
    _gs_tripwire,
    _mock_boundary,
    _mock_build,
    _mock_layer_summary,
    _mock_project,
    _row,
    _run,
    _scenario,
    _series_args,
    _series_row,
    _terrain,
    _tool_json,
)

pytestmark = pytest.mark.asyncio


class TestHandBackUiUrl:
    @respx.mock
    async def test_ui_url_get_project_carries_base_map_and_link(self, _server):
        _mock_project()
        data = json.loads(await _server.get_project(project_id=42))
        assert data["base_map"] == BASE_MAP
        assert data["ui_url"] == f"https://hydrata.example.com/catalogue/#/map/{BASE_MAP}"

    @respx.mock
    async def test_ui_url_create_project_rereads_a_null_base_map(self, _server):
        respx.post(f"{BASE}/projects/").mock(
            return_value=httpx.Response(201, json={"id": 42, "name": "n", "projection": "EPSG:32756", "base_map": None})
        )
        detail = _mock_project()
        data = json.loads(await _server.create_project(name="n", projection="EPSG:32756"))
        assert detail.call_count >= 1
        assert data["base_map"] == BASE_MAP
        _assert_hand_back(data)
        assert data["summary"]["base_map"] == BASE_MAP

    @respx.mock
    async def test_ui_url_finalize_terrain_upload(self, _server):
        _mock_project()
        respx.post(f"{BASE}/projects/42/terrain/upload/finalize/").mock(
            return_value=httpx.Response(202, json=_terrain(110540, "creating"))
        )
        data = json.loads(await _server.finalize_terrain_upload(project_id=42, staging_key=STAGING_KEY))
        _assert_hand_back(data, panel="inputs")
        assert data["summary"]["terrain_id"] == 110540

    @respx.mock
    async def test_ui_url_get_terrain_ready_frames_the_dem_with_its_numbers(self, _server):
        _mock_project()
        ready = _terrain(
            110540,
            "ready",
            gn_layer=1530,
            gn_layer_name="ele_42_utm_dem",
            bbox_wgs84=[150.85, -34.42, 150.92, -34.36],
            dem_elev_min=-1.2,
            dem_elev_max=310.5,
            nodata_fraction=0.0,
            native_crs="EPSG:28356",
        )
        respx.get(f"{BASE}/projects/42/terrain/110540/").mock(return_value=httpx.Response(200, json=ready))
        data = json.loads(await _server.get_terrain(project_id=42, terrain_id=110540, timeout_seconds=0))
        _assert_hand_back(data, panel="inputs", layer="geonode:ele_42_utm_dem")
        assert data["summary"]["dem_elev_max"] == 310.5
        assert data["summary"]["bbox_wgs84"] == [150.85, -34.42, 150.92, -34.36]
        assert "310.5" in data["say_to_user"]

    @respx.mock
    async def test_ui_url_create_time_series_opens_hydrology_with_row_count_and_span(self, _server):
        _mock_project()
        respx.post(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(201, json=_series_row(77)))
        data = json.loads(await _server.create_time_series(**_series_args()))
        _assert_hand_back(data, panel="hydrology")
        assert data["summary"]["row_count"] == 3
        assert data["summary"]["first_timestamp"] == ROW_DATA[0]["timestamp"]
        assert data["summary"]["last_timestamp"] == ROW_DATA[-1]["timestamp"]

    @staticmethod
    def _mock_attach(has_features):
        """A boundary attach whose PATCH answer is the live serializer's row (has_features carried)."""
        _mock_project()
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        respx.get(f"{BASE}/projects/42/boundaries/").mock(
            return_value=httpx.Response(200, json=[_row(27497, "Boundary 01", gn_layer=1400)])
        )
        patched = dict(_row(27497, "Boundary 01", gn_layer=1502), has_features=has_features)
        respx.patch(f"{BASE}/projects/42/boundaries/27497/").mock(return_value=httpx.Response(200, json=patched))
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, BOUNDARY_ALTERNATE))
        )
        _mock_layer_summary(BOUNDARY_ALTERNATE)

    @respx.mock
    async def test_ui_url_attach_input_layer_frames_the_layer_with_api_has_features(self, _server, monkeypatch):
        """TASK-3749: no /gs/ feature count (always refused under a key); has_features is the API's."""
        gs = _gs_tripwire(_server, monkeypatch)
        self._mock_attach(has_features=True)
        data = json.loads(
            await _server.attach_input_layer(
                project_id=42, kind="boundary", execution_id=EXEC_ID, poll_interval_seconds=0
            )
        )
        assert gs == []
        _assert_hand_back(data, panel="inputs", layer=BOUNDARY_ALTERNATE)
        assert data["summary"]["has_features"] is True
        assert "feature_count" not in data["summary"]
        assert data["summary"]["bbox_wgs84"] == [150.89, -34.38, 150.91, -34.37]
        assert data["summary"]["map_updated"] is True
        assert "it has features" in data["say_to_user"] and "review it" in data["say_to_user"]
        assert "unknown" not in data["say_to_user"]
        assert "WARNING" not in data["say_to_user"]

    @respx.mock
    async def test_say_to_user_attach_names_an_empty_layer_from_the_api(self, _server, monkeypatch):
        """TASK-3749: the API says the attached layer is empty, so the user hears it plainly."""
        gs = _gs_tripwire(_server, monkeypatch)
        self._mock_attach(has_features=False)
        data = json.loads(
            await _server.attach_input_layer(
                project_id=42, kind="boundary", execution_id=EXEC_ID, poll_interval_seconds=0
            )
        )
        assert gs == []
        assert data["outcome"] == "attached"
        assert data["summary"]["has_features"] is False
        assert "no features" in data["say_to_user"]

    @respx.mock
    async def test_say_to_user_attach_warns_when_the_map_still_shows_the_old_layer(self, _server):
        """TASK-3188's swallowed MapLayer-swap failure (handover §7c) made client-visible."""
        _mock_project()
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        respx.get(f"{BASE}/projects/42/boundaries/").mock(
            return_value=httpx.Response(200, json=[_row(27497, "Boundary 01", gn_layer=1400)])
        )
        respx.patch(f"{BASE}/projects/42/boundaries/27497/").mock(
            return_value=httpx.Response(200, json=_row(27497, "Boundary 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, BOUNDARY_ALTERNATE))
        )
        _mock_layer_summary(BOUNDARY_ALTERNATE, on_map=False)
        data = json.loads(
            await _server.attach_input_layer(
                project_id=42, kind="boundary", execution_id=EXEC_ID, poll_interval_seconds=0
            )
        )
        assert data["outcome"] == "attached"
        assert data["summary"]["map_updated"] is False
        assert "WARNING" in data["say_to_user"]

    @respx.mock
    async def test_attach_input_layer_reports_the_converted_layer_the_row_now_uses(self, _server):
        """TASK-3918/TASK-4041: the v2 PATCH converts the upload (1502) into the kind's
        canonical table (1600) and the row points at THAT; the map carries 1600 only.
        Reporting the raw upload gave the wrong typename and a false 'map does not show
        the new layer' WARNING on every kind but culvert (28-10-26, project 1114)."""
        _mock_project()
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        respx.get(f"{BASE}/projects/42/boundaries/").mock(
            return_value=httpx.Response(200, json=[_row(27497, "Boundary 01", gn_layer=1400)])
        )
        patch_route = respx.patch(f"{BASE}/projects/42/boundaries/27497/").mock(
            return_value=httpx.Response(200, json=dict(_row(27497, "Boundary 01", gn_layer=1600), has_features=True))
        )
        raw_route = respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:sullivans_boundary"))
        )
        converted_alternate = "geonode:bdy_42_boundary_01_dfec"
        respx.get(f"{ORIGIN}/api/v2/datasets/1600/").mock(
            return_value=httpx.Response(200, json=_dataset(1600, converted_alternate))
        )
        _mock_layer_summary(converted_alternate)
        data = json.loads(
            await _server.attach_input_layer(
                project_id=42, kind="boundary", execution_id=EXEC_ID, poll_interval_seconds=0
            )
        )
        assert json.loads(patch_route.calls[0].request.content) == {"gn_layer": 1502}
        assert data["outcome"] == "attached"
        assert data["dataset_pk"] == 1600 and data["gn_layer"] == 1600
        assert data["source_dataset_pk"] == 1502
        assert data["dataset_alternate"] == converted_alternate
        assert data["summary"]["layer"] == converted_alternate
        assert data["summary"]["map_updated"] is True
        assert "WARNING" not in data["say_to_user"]
        assert not raw_route.called

    @respx.mock
    async def test_ui_url_create_scenario_opens_the_scenario_with_estimate_and_price(self, _server):
        _mock_project()
        respx.post(f"{BASE}/projects/42/scenarios/").mock(return_value=httpx.Response(201, json={"id": 5}))
        respx.get(SCENARIO_URL).mock(
            return_value=httpx.Response(200, json=_scenario(compute_cost_estimate=1.23, vcpu_hours_estimate=0.4))
        )
        data = json.loads(await _server.create_scenario(project_id=42, name="s", resolution=36.0, duration=3600))
        _assert_hand_back(data, panel="scenarios", scenario=5)
        assert data["summary"]["mesh_triangle_count_estimate"] == 19_729
        assert data["summary"]["compute_cost_estimate"] == 1.23
        assert "19,729" in data["say_to_user"] and "US$1.23" in data["say_to_user"]

    @respx.mock
    async def test_ui_url_build_scenario_built_reports_mesh_and_actual_cost(self, _server):
        _mock_project()
        _detail_to_built(_scenario())
        _mock_boundary(True)
        _mock_build()
        respx.get(f"{BASE}/runs/501/").mock(
            return_value=httpx.Response(
                200, json=_run(501, "built", mesh_triangle_count=20_480, mesh_actual_cost_estimate=2.5)
            )
        )
        data = _tool_json(await _build(_server))
        assert data["outcome"] == "built"
        _assert_hand_back(data, panel="scenarios", scenario=5)
        assert data["summary"]["mesh_actual_cost_estimate"] == 2.5
        assert "start the simulation" in data["say_to_user"]

    @respx.mock
    async def test_ui_url_start_simulation_opens_the_run(self, _server):
        _mock_project()
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(202, json=_run(502, "queued")))
        data = json.loads(await _server.start_simulation(scenario_id=5))
        _assert_hand_back(data, run=502)
        assert data["summary"]["run_id"] == 502

    @respx.mock
    async def test_ui_url_cancel_run_reads_the_run_for_its_project(self, _server):
        _mock_project()
        respx.post(f"{BASE}/runs/502/cancel/").mock(
            return_value=httpx.Response(200, json={"id": 502, "status": "cancelled"})
        )
        respx.get(f"{BASE}/runs/502/").mock(return_value=httpx.Response(200, json=_run(502, "cancelled")))
        data = json.loads(await _server.cancel_run(run_id=502))
        _assert_hand_back(data, panel="scenarios", scenario=5)

    @respx.mock
    async def test_ui_url_retry_run_opens_the_rebuilding_scenario(self, _server):
        _mock_project()
        respx.post(f"{BASE}/runs/502/retry/").mock(
            return_value=httpx.Response(202, json={"id": 502, "status": "created", "rebuilding": True})
        )
        respx.get(f"{BASE}/runs/502/").mock(return_value=httpx.Response(200, json=_run(502, "created")))
        data = json.loads(await _server.retry_run(run_id=502))
        _assert_hand_back(data, panel="scenarios", scenario=5)
        assert data["summary"]["rebuilding"] is True

    @respx.mock
    async def test_ui_url_lookup_failure_never_hides_the_change(self, _server):
        """Best effort: the create landed; a failed base-map read leaves ui_url null and says so."""
        respx.post(f"{BASE}/scenarios/5/run/").mock(return_value=httpx.Response(202, json=_run(502, "queued")))
        respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(500))
        data = json.loads(await _server.start_simulation(scenario_id=5))
        assert data["id"] == 502 and data["ui_url"] is None
        assert "hand_back_error" in data and "No map link" in data["say_to_user"]

    async def test_ui_url_origin_is_the_public_host_not_the_loopback_api(self, _server, monkeypatch):
        """Prod's API URL is the loopback 127.0.0.1:8081 listener; the human needs https://<host>."""
        # `_ui_origin` reads `config` in the module that defines it (_helpers).
        monkeypatch.setattr(
            "hydrata_mcp._helpers.config",
            _server.Config(api_url="http://127.0.0.1:8081/api/v2/anuga", api_host="hydrata.com"),
        )
        assert _server._ui_url(7, panel="inputs", layer="geonode:a_b") == (
            "https://hydrata.com/catalogue/#/map/7?panel=inputs&layer=geonode:a_b"
        )
        monkeypatch.setattr(
            "hydrata_mcp._helpers.config",
            _server.Config(api_url="http://localhost/api/v2/anuga", ui_origin="http://localhost:8081"),
        )
        assert _server._ui_url(7, run=9) == "http://localhost:8081/catalogue/#/map/7?run=9"
        assert _server._ui_url(None, panel="inputs") is None


class TestHandBackRefusals:
    @respx.mock
    async def test_say_to_user_no_boundary_refusal_routes_to_the_map(self, _server):
        _mock_project()
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(boundary=None)))
        build = _mock_build()
        data = _tool_json(await _build(_server))
        assert data["outcome"] == "refused" and not build.called
        _assert_hand_back(data, panel="inputs")
        assert "draw it in the map" in data["say_to_user"]

    @respx.mock
    async def test_say_to_user_boundary_without_features_frames_that_boundary(self, _server):
        _mock_project()
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        respx.get(BOUNDARY_URL).mock(
            return_value=httpx.Response(
                200, json={"id": 27497, "gn_layer": 1524, "gn_layer_name": "bdy_42_boundary_01", "has_features": False}
            )
        )
        build = _mock_build()
        data = _tool_json(await _build(_server))
        assert data["outcome"] == "refused" and not build.called
        _assert_hand_back(data, panel="inputs", layer=BOUNDARY_ALTERNATE)
        assert "draw it in the map" in data["say_to_user"]

    @respx.mock
    async def test_say_to_user_relayed_422_boundary_no_external_says_fix_it_in_the_map(self, _server):
        _mock_project()
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        respx.get(BOUNDARY_URL).mock(
            return_value=httpx.Response(
                200, json={"id": 27497, "gn_layer": 1524, "gn_layer_name": "bdy_42_boundary_01", "has_features": True}
            )
        )
        refusal = {
            "error_code": "BOUNDARY_NO_EXTERNAL",
            "detail": "Every boundary line is Location=Internal: features 1 (…), 2 (…).",
        }
        _mock_build(422, refusal)
        data = _tool_json(await _build(_server))
        assert data["error_code"] == "BOUNDARY_NO_EXTERNAL" and data["http_status"] == 422
        assert data["detail"] == refusal["detail"]  # the body is still relayed verbatim
        _assert_hand_back(data, panel="inputs", layer=BOUNDARY_ALTERNATE)
        assert "fix it in the map" in data["say_to_user"]

    @pytest.mark.parametrize("kind", ["breakline", "culvert"])
    @respx.mock
    async def test_say_to_user_breakline_culvert_refusal_has_ui_url_but_no_draw_hint(self, _server, kind):
        """TASK-3641 (epic 3585) INVERTED the culvert case: only breakline is still
        refused with the 'not supported' hand-back; a culvert attach goes ahead
        (a failed import here) and never says 'not supported'. Name kept (ratchet)."""
        project = _mock_project()
        writes = [
            respx.post(url__startswith=BASE).mock(return_value=httpx.Response(201, json={})),
            respx.patch(url__startswith=BASE).mock(return_value=httpx.Response(200, json={})),
        ]
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("failed", log="bad")))
        if kind == "breakline":
            with pytest.raises(_server.ToolError) as exc_info:
                await _server.attach_input_layer(project_id=42, kind=kind, execution_id=EXEC_ID)
            text = str(exc_info.value)
            assert project.called and not any(w.called for w in writes)  # one read, no write
            assert f"https://hydrata.example.com/catalogue/#/map/{BASE_MAP}?panel=inputs" in text
            assert "not supported" in text
            assert "draw it in the map" not in text
        else:
            data = json.loads(
                await _server.attach_input_layer(
                    project_id=42, kind=kind, execution_id=EXEC_ID, poll_interval_seconds=0
                )
            )
            assert data["outcome"] == "upload_failed" and not any(w.called for w in writes)
            assert "not supported" not in data["say_to_user"]
            assert f"https://hydrata.example.com/catalogue/#/map/{BASE_MAP}?panel=inputs" == data["ui_url"]
