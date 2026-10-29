"""Scenario tools: get_scenario, create_scenario and build_scenario."""

import inspect
import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    BUILD_URL,
    CALLER_KEY,
    ELIDED,
    ESTIMATE_BREAKDOWN,
    HAND_BACK_KEYS,
    KEY_HEADERS,
    RUN_S3_URL_KEYS,
    SCENARIO_URL,
    SUMMARY_HYDROGRAPH,
    _asgi_client,
    _build,
    _call_as_caller,
    _detail_to_built,
    _mock_boundary,
    _mock_build,
    _parse,
    _presigned_run,
    _rpc,
    _run,
    _scenario,
    _signed_url,
    _tool_error_text,
    _tool_json,
)

pytestmark = pytest.mark.asyncio


class TestGetScenario:
    @respx.mock
    async def test_returns_scenario(self, _server):
        scenario = {"id": 5, "computed_status": "built"}
        respx.get(f"{BASE}/projects/1/scenarios/5/").mock(return_value=httpx.Response(200, json=scenario))
        result = await _server.get_scenario(project_id=1, scenario_id=5)
        assert json.loads(result)["computed_status"] == "built"


def _without_hand_back(data):
    """The API body a tool relays, minus TASK-3203's hand-back keys (which must be present)."""
    assert {"ui_url", "summary", "say_to_user"} <= set(data), sorted(data)
    return {k: v for k, v in data.items() if k not in HAND_BACK_KEYS}


class TestGetScenarioHydrologyCheck:
    @respx.mock
    async def test_hydrology_check_is_passed_through_verbatim_when_the_server_sends_it(self, _server):
        check = {
            "features": [{"kind": "rainfall", "feature": "Gauge 1", "summary": SUMMARY_HYDROGRAPH}],
            "warnings": [{"code": "PEAK_OUTSIDE_WINDOW", "message": "peak is after the model ends"}],
        }
        respx.get(f"{BASE}/projects/42/scenarios/7/").mock(
            return_value=httpx.Response(200, json={"id": 7, "computed_status": "created", "hydrology_check": check})
        )
        data = _tool_json(await _call_as_caller(_server, "get_scenario", {"project_id": 42, "scenario_id": 7}))
        assert data["hydrology_check"] == check

    @respx.mock
    async def test_hydrology_check_absent_stays_absent_not_null(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/7/").mock(
            return_value=httpx.Response(200, json={"id": 7, "computed_status": "created"})
        )
        data = _tool_json(await _call_as_caller(_server, "get_scenario", {"project_id": 42, "scenario_id": 7}))
        assert "hydrology_check" not in data


class TestGetScenarioUnboundSeries:
    """TASK-3213 AC5: get_scenario warns, naming each project series no inflow or rainfall feature uses."""

    CHECK = {
        "features": [
            {"layer_kind": "inflow", "feature_id": "inf.1", "series": {"id": 7, "name": "Creek hydrograph"}},
            {"layer_kind": "rainfall", "feature_id": "rai.1", "series": {"id": 6, "name": "Design storm"}},
        ],
        "warnings": [],
    }

    @staticmethod
    def _listing(*rows):
        return {
            "count": len(rows),
            "results": [
                {"id": i, "name": n, "series_type": t, "units": "", "timezone": "UTC", "data": {"rowData": []}}
                for i, n, t in rows
            ],
        }

    async def _scenario(self, _server, check, listing):
        respx.get(f"{BASE}/projects/42/scenarios/7/").mock(
            return_value=httpx.Response(200, json={"id": 7, "computed_status": "created", "hydrology_check": check})
        )
        respx.get(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(200, json=listing))
        return _tool_json(await _call_as_caller(_server, "get_scenario", {"project_id": 42, "scenario_id": 7}))

    @respx.mock
    async def test_unbound_series_names_each_series_no_feature_uses(self, _server):
        data = await self._scenario(
            _server,
            self.CHECK,
            self._listing(
                (7, "Creek hydrograph", "hydrograph"),
                (6, "Design storm", "hyetograph"),
                (9, "Orphan gauge", "hyetograph"),
                (10, "Old flow", "hydrograph"),
            ),
        )
        assert data["unbound_series"] == [
            {"id": 9, "name": "Orphan gauge", "series_type": "hyetograph"},
            {"id": 10, "name": "Old flow", "series_type": "hydrograph"},
        ]
        assert "'Orphan gauge'" in data["say_to_user"] and "'Old flow'" in data["say_to_user"]
        assert "'Creek hydrograph'" not in data["say_to_user"]

    @respx.mock
    async def test_unbound_series_every_series_used_adds_nothing(self, _server):
        """NEGATIVE CONTROL: no warning, no key, no say_to_user."""
        data = await self._scenario(
            _server, self.CHECK, self._listing((7, "Creek hydrograph", "hydrograph"), (6, "Design storm", "hyetograph"))
        )
        assert "unbound_series" not in data and "say_to_user" not in data

    @respx.mock
    async def test_unbound_series_a_stage_series_is_a_boundary_input_and_not_checked(self, _server):
        data = await self._scenario(
            _server, self.CHECK, self._listing((7, "Creek hydrograph", "hydrograph"), (8, "Tide", "stage"))
        )
        assert "unbound_series" not in data

    @respx.mock
    async def test_unbound_series_an_unreadable_layer_means_no_guess(self, _server):
        check = {**self.CHECK, "warnings": [{"error_code": "LAYER_UNREADABLE", "layer_kind": "rainfall"}]}
        data = await self._scenario(_server, check, self._listing((9, "Orphan gauge", "hyetograph")))
        assert "unbound_series" not in data

    @respx.mock
    async def test_unbound_series_a_failed_series_listing_never_breaks_get_scenario(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/7/").mock(
            return_value=httpx.Response(
                200, json={"id": 7, "computed_status": "created", "hydrology_check": self.CHECK}
            )
        )
        respx.get(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(500, json={"detail": "x"}))
        data = _tool_json(await _call_as_caller(_server, "get_scenario", {"project_id": 42, "scenario_id": 7}))
        assert data["scenario_id"] == 7 and "unbound_series" not in data

    @respx.mock
    async def test_unbound_series_without_a_hydrology_check_makes_no_listing_call(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/7/").mock(
            return_value=httpx.Response(200, json={"id": 7, "computed_status": "created"})
        )
        listing = respx.get(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(200, json={}))
        await _call_as_caller(_server, "get_scenario", {"project_id": 42, "scenario_id": 7})
        assert not listing.called


MESH_TOO_LARGE = {
    "error_code": "MESH_TOO_LARGE",
    "estimate": 25_568_180,
    "ceiling": 7_737_436,
    "detail": (
        "This mesh would be ~25,568,180 triangles, above the 7,737,436-triangle "
        "limit. Coarsen the resolution or reduce the extent."
    ),
}
DEDUP_409 = {"status": "building", "run_id": 501, "detail": "A build is already in progress for this scenario."}


class TestCreateScenarioTool:
    @respx.mock
    async def test_create_scenario_posts_the_write_fields_and_reports_the_estimate_from_the_detail_get(self, _server):
        created = {
            "id": 5,
            "name": "mcp-w1-3172-t",
            "description": "",
            "terrain": 110533,
            "boundary": 27497,
            "friction": 124557,
            "inflow": 124555,
            "rainfall": 124556,
            "building": 124558,
            "mesh_region": None,
            "network": None,
            "resolution": 36.0,
            "duration": 43200,
        }
        post_route = respx.post(f"{BASE}/projects/42/scenarios/").mock(return_value=httpx.Response(201, json=created))
        detail_route = respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        result = await _call_as_caller(
            _server,
            "create_scenario",
            {
                "project_id": 42,
                "name": "mcp-w1-3172-t",
                "resolution": 36,
                "duration": 43200,
                "terrain": 110533,
                "boundary": 27497,
                "friction": 124557,
                "inflow": 124555,
                "rainfall": 124556,
                "building": 124558,
            },
        )
        assert post_route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert detail_route.calls[0].request.headers["authorization"] == CALLER_KEY
        # Exactly ScenarioCreateSerializerV2's writable fields the tool exposes
        # (network is not offered); an unset FK travels as null.
        assert json.loads(post_route.calls[0].request.content) == {
            "name": "mcp-w1-3172-t",
            "description": "",
            "resolution": 36,
            "duration": 43200,
            "terrain": 110533,
            "boundary": 27497,
            "friction": 124557,
            "inflow": 124555,
            "rainfall": 124556,
            "building": 124558,
            "mesh_region": None,
        }
        data = _tool_json(result)
        assert data["id"] == 5
        assert data["http_status"] == 201
        # The estimate comes from the DETAIL — the create response has none.
        assert "mesh_triangle_count_estimate" not in created
        assert data["mesh_triangle_count_estimate"] == 19_729
        assert data["mesh_triangle_count_estimate_breakdown"]["total"] == 19_729
        assert data["computed_status"] == "created"
        assert data["mesh_region"] is None
        assert "latest_run" not in data and "perms" not in data


class TestBuildScenarioTool:
    @respx.mock
    async def test_build_scenario_under_cap_posts_build_then_polls_computed_status_to_built(self, _server):
        detail_route = _detail_to_built(_scenario())
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert build_route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert boundary_route.calls[0].request.headers["authorization"] == CALLER_KEY
        for call in detail_route.calls:
            assert call.request.headers["authorization"] == CALLER_KEY
        assert detail_route.call_count == 4  # pre-check, then created → building → built
        assert data["outcome"] == "built"
        assert data["computed_status"] == "built"
        assert data["posted"] is True
        assert data["http_status"] == 202
        assert data["build"] == {"status": "created", "run_id": 501, "scenario_id": 5}
        assert data["run_id"] == 501
        assert data["run_status"] == "built"
        assert data["mesh_triangle_count"] == 19_812
        assert data["mesh_triangle_count_estimate"] == 19_729
        assert data["polls"] == 3
        # Compact: never the whole detail (latest_run carries the full build log).
        assert "latest_run" not in data and "log" not in data

    @respx.mock
    async def test_build_scenario_over_100k_without_confirm_refuses_and_never_posts(self, _server):
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(estimate=255_412)))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called
        assert data["outcome"] == "refused"
        assert data["posted"] is False
        assert data["mesh_triangle_count_estimate"] == 255_412
        assert "255,412" in data["reason"] and "confirm=true" in data["reason"]

    @respx.mock
    async def test_build_scenario_over_100k_with_confirm_true_posts(self, _server):
        _detail_to_built(_scenario(estimate=255_412))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server, confirm=True))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"

    @respx.mock
    async def test_build_scenario_refuses_a_null_boundary_without_posting(self, _server):
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(boundary=None, estimate=0)))
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called and not boundary_route.called
        assert data["outcome"] == "refused"
        assert "boundary has no features" in data["reason"]

    @respx.mock
    async def test_build_scenario_refuses_a_boundary_without_features_without_posting(self, _server):
        """The default 'Boundary 01' row before attach_input_layer: the server ADMITS
        the build and errors at make_package — refuse here, naming the row."""
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(estimate=0)))
        boundary_route = _mock_boundary(False)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert boundary_route.called
        assert not build_route.called
        assert data["outcome"] == "refused"
        assert "boundary has no features" in data["reason"] and "27497" in data["reason"]

    @respx.mock
    async def test_build_scenario_estimate_zero_with_features_proceeds_to_post(self, _server):
        """Estimate 0 is NOT 'no boundary': int(round(...)) of a coarse resolution over
        a real boundary is 0 and BUILDS (localhost scenario 86955: 8 triangles)."""
        _detail_to_built(_scenario(estimate=0))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"

    @respx.mock
    async def test_build_scenario_refuses_when_the_estimate_is_none_without_posting(self, _server):
        """resolution 0 (FloatField default 100, not nullable) → estimate None."""
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(estimate=None, resolution=0.0)))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called
        assert data["outcome"] == "refused"
        assert "resolution is 0/unset" in data["reason"]

    @respx.mock
    async def test_build_scenario_returns_the_422_mesh_too_large_body_verbatim_as_a_non_error(self, _server):
        detail_route = respx.get(SCENARIO_URL).mock(
            return_value=httpx.Response(200, json=_scenario(estimate=25_568_180, resolution=1.0))
        )
        _mock_boundary(True)
        build_route = _mock_build(422, MESH_TOO_LARGE)
        result = await _build(_server, confirm=True)
        assert result.get("isError") is not True, result["content"][0]["text"]
        data = _tool_json(result)
        assert build_route.call_count == 1
        assert _without_hand_back(data) == {**MESH_TOO_LARGE, "http_status": 422}
        assert detail_route.call_count == 1  # nothing to poll: no Run was created

    @respx.mock
    async def test_build_scenario_returns_a_409_with_error_code_verbatim_and_does_not_poll(self, _server):
        unavailable = {
            "error_code": "COMPUTE_TARGET_UNAVAILABLE",
            "detail": "ANUGA_BATCH_GPU_L40S_MEMORY_LIMIT_MIB is not a positive integer",
        }
        detail_route = respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        _mock_build(409, unavailable)
        data = _tool_json(await _build(_server))
        assert _without_hand_back(data) == {**unavailable, "http_status": 409}
        assert detail_route.call_count == 1

    @respx.mock
    async def test_build_scenario_non_json_4xx_body_becomes_response_text(self, _server):
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        respx.post(BUILD_URL).mock(
            return_value=httpx.Response(403, text="<html>Forbidden</html>", headers={"content-type": "text/html"})
        )
        data = _tool_json(await _build(_server))
        assert _without_hand_back(data) == {"response": "<html>Forbidden</html>", "http_status": 403}

    @respx.mock
    async def test_build_scenario_5xx_on_build_is_still_a_tool_error(self, _server):
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario()))
        _mock_boundary(True)
        respx.post(BUILD_URL).mock(return_value=httpx.Response(502, text="<html>Bad Gateway</html>"))
        text = _tool_error_text(await _build(_server))
        assert "502" in text
        assert "Bad Gateway" not in text  # 5xx bodies are HTML, never relayed

    @pytest.mark.parametrize("in_flight", ["created", "building"])
    @respx.mock
    async def test_build_scenario_resumes_polling_when_latest_run_is_in_flight_without_posting(
        self, _server, in_flight
    ):
        detail_route = respx.get(SCENARIO_URL).mock(
            side_effect=[
                httpx.Response(200, json=_scenario(in_flight, latest_run=_run(501, in_flight))),
                httpx.Response(200, json=_scenario("building", latest_run=_run(501, "building"))),
                httpx.Response(200, json=_scenario("built", latest_run=_run(501, "built"), latest_run_is_valid=True)),
            ]
        )
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called
        assert not boundary_route.called  # the re-call checks run BEFORE the spend gates
        assert detail_route.call_count == 3
        assert data["outcome"] == "built"
        assert data["posted"] is False
        assert data["run_id"] == 501

    @respx.mock
    async def test_build_scenario_resumes_polling_on_the_409_dedup_body_without_a_second_post(self, _server):
        """The pre-check saw no run but a build was dispatched before the POST landed:
        the dedup body has NO error_code — poll THAT run, never POST again."""
        detail_route = respx.get(SCENARIO_URL).mock(
            side_effect=[
                httpx.Response(200, json=_scenario()),
                httpx.Response(200, json=_scenario("building", latest_run=_run(501, "building"))),
                httpx.Response(200, json=_scenario("built", latest_run=_run(501, "built"), latest_run_is_valid=True)),
            ]
        )
        _mock_boundary(True)
        build_route = _mock_build(409, DEDUP_409)
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert detail_route.call_count == 3
        assert data["outcome"] == "built"
        assert data["http_status"] == 409
        assert data["build"] == DEDUP_409
        assert data["run_id"] == 501

    @pytest.mark.parametrize("done", ["built", "complete"])
    @respx.mock
    async def test_build_scenario_built_or_complete_without_rebuild_returns_the_state_and_never_posts(
        self, _server, done
    ):
        """`complete` is NOT dedup-blocked server-side (BUILD_DEDUP_BLOCKING_STATUS_VALUES =
        in-flight + created): a blind re-POST would dispatch a duplicate build."""
        detail_route = respx.get(SCENARIO_URL).mock(
            return_value=httpx.Response(
                200,
                json=_scenario(done, latest_run=_run(501, done, mesh_triangle_count=19_812), latest_run_is_valid=True),
            )
        )
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called and not boundary_route.called
        assert detail_route.call_count == 1
        assert data["outcome"] == done
        assert data["computed_status"] == done
        assert data["posted"] is False
        assert data["run_id"] == 501
        assert data["mesh_triangle_count"] == 19_812
        assert "rebuild=true" in data["note"]

    @respx.mock
    async def test_build_scenario_built_with_rebuild_true_posts_a_new_build(self, _server):
        _detail_to_built(_scenario("built", latest_run=_run(500, "built"), latest_run_is_valid=True))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server, rebuild=True))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"
        assert data["run_id"] == 501

    @respx.mock
    async def test_build_scenario_built_but_latest_run_is_valid_false_posts_a_rebuild(self, _server):
        """Every PATCH sets latest_run_is_valid False (api_v2.py:3095-3101): the built
        package is stale against the current inputs — rebuild without rebuild=true."""
        _detail_to_built(_scenario("built", latest_run=_run(500, "built"), latest_run_is_valid=False))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"
        assert data["run_id"] == 501

    @pytest.mark.parametrize("dead", ["error", "cancelled"])
    @respx.mock
    async def test_build_scenario_after_an_error_or_cancelled_run_posts_a_rebuild(self, _server, dead):
        _detail_to_built(_scenario(dead, latest_run=_run(500, dead, error_message="boom")))
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert build_route.call_count == 1
        assert data["outcome"] == "built"
        assert data["run_id"] == 501

    @respx.mock
    async def test_build_scenario_refuses_a_stranger_read_without_posting(self, _server):
        """A public project's scenario read by a non-member is 200 with ONLY
        STRANGER_SCENARIO_FIELDS (serializers_v2.py:395-405): no boundary, no
        computed_status. The build needs EDITOR — say so instead of guessing."""
        stranger = {
            "id": 5,
            "name": "mcp-w1-3172-t",
            "description": "",
            "resolution": 36.0,
            "duration": 43200,
            "mesh_triangle_count_estimate": 19_729,
            "mesh_triangle_count_estimate_breakdown": ESTIMATE_BREAKDOWN,
            "latest_run": None,
            "latest_complete_run": None,
        }
        respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=stranger))
        boundary_route = _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server))
        assert not build_route.called and not boundary_route.called
        assert data["outcome"] == "refused"
        assert "not a project member" in data["reason"] and "EDITOR" in data["reason"]

    @respx.mock
    async def test_build_scenario_timeout_is_bounded_and_names_the_run_id(self, _server):
        """timeout_seconds=0 → the POST, then exactly ONE poll, then `timed_out` naming
        the run so the agent can cancel_run one stuck in `created`; a re-call resumes."""
        detail_route = respx.get(SCENARIO_URL).mock(
            side_effect=[
                httpx.Response(200, json=_scenario()),
                httpx.Response(200, json=_scenario("created", latest_run=_run(501, "created"))),
                httpx.Response(500),
            ]
        )
        _mock_boundary(True)
        build_route = _mock_build()
        data = _tool_json(await _build(_server, timeout_seconds=0))
        assert build_route.call_count == 1
        assert detail_route.call_count == 2
        assert data["outcome"] == "timed_out"
        assert data["computed_status"] == "created"
        assert data["run_id"] == 501
        assert data["polls"] == 1
        assert "cancel_run" in data["note"]

    @respx.mock
    async def test_build_scenario_error_end_state_carries_the_run_error_message(self, _server):
        respx.get(SCENARIO_URL).mock(
            side_effect=[
                httpx.Response(200, json=_scenario()),
                httpx.Response(
                    200,
                    json=_scenario(
                        "error", latest_run=_run(501, "error", error_message="Could not find Boundary data")
                    ),
                ),
            ]
        )
        _mock_boundary(True)
        _mock_build()
        data = _tool_json(await _build(_server))
        assert data["outcome"] == "error"
        assert data["run_id"] == 501
        assert data["error_message"] == "Could not find Boundary data"

    async def test_build_scenario_and_create_scenario_listed_with_computed_status_metres_and_confirm(self, _server):
        """The docstring ACs, made provable: the units rule (a LENGTH in metres on the
        scenario AND on a MeshRegion feature since TASK-3186, with the marker the
        build requires), the poll key and the confirm gate are on the tools' OWN
        descriptions; get_scenario's names computed_status."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        build = tools["build_scenario"]["description"]
        for token in ("computed_status", "resolution", "metres", "confirm", "100,000", "MESH_TOO_LARGE"):
            assert token in build, token
        create = tools["create_scenario"]["description"]
        for token in (
            "resolution",
            "metres",
            "resolution²/2",
            "computed_status",
            "MeshRegion",
            "resolution_units = m",
            "MESH_REGION_UNITS_UNMARKED",
        ):
            assert token in create, token
        assert "AREA" not in create and "AREA" not in build  # the pre-3186 split is gone
        get_scenario = tools["get_scenario"]["description"]
        assert "computed_status" in get_scenario
        props = tools["build_scenario"]["inputSchema"]["properties"]
        assert "confirm" in props and "rebuild" in props and "timeout_seconds" in props
        assert (
            len(tools) == 36
        )  # +TASK-3964 suggest_capability; +TASK-4059 list_terrain_sources + create_terrain_from_source; +TASK-4058 request_input_upload; the module docstring's count (+ TASK-4052 write_input_layer, TASK-4056 update/delete_time_series, TASK-4057 describe_input_kinds)


class TestGetScenarioCompactState:
    """AC1: the compact state, and never a presigned URL."""

    @staticmethod
    def _detail():
        return _scenario(
            "complete",
            latest_run=_presigned_run(67181, "complete", mesh_triangle_count=256_869),
            latest_complete_run=_presigned_run(67181, "complete", mesh_triangle_count=256_869),
            latest_run_is_valid=True,
            resolution=35.0,
            terrain=110535,
            boundary=27499,
            compute_cost_estimate=0.52,
            vcpu_hours_estimate=3.071667015,
            inflow_anchor_mismatch=None,
        )

    @respx.mock
    async def test_get_scenario_elides_the_ten_presigned_urls_of_both_run_blocks(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(return_value=httpx.Response(200, json=self._detail()))
        result = await _server.get_scenario(project_id=42, scenario_id=5)
        assert "X-Amz" not in result
        for key in RUN_S3_URL_KEYS:
            assert key not in result, key
        data = json.loads(result)
        # The compact state replaces the detail wholesale: no run blocks, no log.
        assert "latest_run" not in data and "latest_complete_run" not in data
        assert "log" not in result and "perms" not in data

    @respx.mock
    async def test_get_scenario_carries_the_state_the_inputs_and_the_price(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(return_value=httpx.Response(200, json=self._detail()))
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        # _build_state's ten flat keys (UNFORKED).
        assert data["scenario_id"] == 5
        assert data["computed_status"] == "complete"
        assert data["latest_run_is_valid"] is True
        assert data["run_id"] == 67181
        assert data["run_status"] == "complete"
        assert data["mesh_triangle_count"] == 256_869
        assert data["error_message"] is None
        assert data["user_message"] is None
        assert data["status_detail"] is None
        assert data["mesh_triangle_count_estimate"] == 19_729
        # …plus the estimate breakdown, the inputs and the price: without them a
        # session that did not create the scenario would be blind to what it is
        # about to build and what it will cost (there is no list_scenarios tool).
        assert data["mesh_triangle_count_estimate_breakdown"]["total"] == 19_729
        assert data["name"] == "mcp-w1-3172-t"
        assert data["project"] == 42
        assert data["resolution"] == 35.0
        assert data["duration"] == 43200
        assert data["terrain"] == 110535
        assert data["boundary"] == 27499
        assert data["friction"] == 124557
        assert data["inflow"] == 124555
        assert data["rainfall"] == 124556
        assert data["building"] == 124558
        assert data["mesh_region"] is None
        assert data["description"] == ""
        assert data["compute_cost_estimate"] == 0.52
        assert data["vcpu_hours_estimate"] == 3.071667015
        # The scenario's only pre-build correctness warning: present even when None.
        assert "inflow_anchor_mismatch" in data

    @respx.mock
    async def test_get_scenario_redacts_a_presigned_url_quoted_in_the_description(self, _server):
        """The compact shape alone would hide a leak: the whitelist copies the
        scenario's own text through, so get_scenario must ALSO go through the
        elision helper (the description is free text a user can paste into)."""
        detail = _scenario(
            "built",
            description="rebuilt from " + _signed_url("runs/67179/package.zip") + " on 2026-09-21",
        )
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(return_value=httpx.Response(200, json=detail))
        result = await _server.get_scenario(project_id=42, scenario_id=5)
        assert "X-Amz" not in result
        assert json.loads(result)["description"] == f"rebuilt from {ELIDED} on 2026-09-21"

    @respx.mock
    async def test_get_scenario_does_not_fabricate_nulls_for_a_non_member_record(self, _server):
        """STRANGER_SCENARIO_FIELDS (hydrata serializers_v2.py:381-400) DROPS the
        FKs, the estimates and the perms from a non-member's read. `.get(key)`
        would turn "withheld" into "terrain: null, compute_cost_estimate: null" —
        i.e. "no terrain, no cost". Absent stays absent."""
        stranger = {"id": 5, "name": "mcp-w1-3172-t", "project": 42, "description": ""}
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(return_value=httpx.Response(200, json=stranger))
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        assert data["scenario_id"] == 5 and data["name"] == "mcp-w1-3172-t"
        for withheld in (
            "terrain",
            "boundary",
            "compute_cost_estimate",
            "vcpu_hours_estimate",
            "resolution",
            "duration",
            "inflow_anchor_mismatch",
        ):
            assert withheld not in data, withheld

    async def test_get_scenario_says_the_inflow_lead_in_gap_is_zero_flow(self, _server):
        """TASK-3978: before a later inflow series' first sample the engine applies 0
        (run_anuga run_utils.py fillna(0.0), TASK-2825); it never holds the first
        value backwards to the model start."""

        doc = " ".join(inspect.getdoc(_server.get_scenario).split())
        assert "zero flow from that series over the lead-in gap" in doc
        source = _server.module_source()
        for stale in ("held backwards", "ffilled backwards", "repeats its first value"):
            assert stale not in source, stale

    @respx.mock
    async def test_get_scenario_tolerates_a_non_dict_detail(self, _server):
        respx.get(f"{BASE}/projects/42/scenarios/5/").mock(return_value=httpx.Response(200, json="gone"))
        data = json.loads(await _server.get_scenario(project_id=42, scenario_id=5))
        assert data["scenario_id"] is None and data["computed_status"] is None


# ---------------------------------------------------------------------------
# W0 simplify-pass (Phase 1.7 cumulative sweep, epic 3200) — the elision lives
# in `_build_state`, so EVERY tool that answers with a run's state is covered
# once: get_scenario (TASK-3187), build_scenario's poll/short-circuit returns
# and `_build_refusal`. Before this, get_scenario elided and build_scenario —
# which composes the SAME helper from the SAME detail, and additionally folds
# the raw build POST body in under `build=` — did not.
# ---------------------------------------------------------------------------
class TestBuildScenarioElidesPresignedUrls:
    @respx.mock
    async def test_build_scenario_elides_a_presigned_url_quoted_in_the_run_error_message(self, _server):
        """`_build_state` copies the run's `error_message` / `user_message` /
        `status_detail` verbatim. They are server-composed free text, so a
        future line quoting a signed URL would reach the transcript through
        build_scenario even though get_scenario elides the same field."""
        signed = _signed_url("runs/67181/package.zip")
        respx.get(SCENARIO_URL).mock(
            return_value=httpx.Response(
                200,
                json=_scenario(
                    "built",
                    latest_run=_run(501, "built", error_message=f"package upload failed for {signed}"),
                    latest_run_is_valid=True,
                ),
            )
        )
        data = _tool_json(await _build(_server))
        assert "X-Amz" not in json.dumps(data)
        assert data["error_message"] == f"package upload failed for {ELIDED}"
        assert data["posted"] is False

    @respx.mock
    async def test_build_scenario_refusal_elides_a_presigned_url_it_echoes(self, _server):
        """The refusal path (`_build_refusal`) goes through the same helper."""
        signed = _signed_url("runs/67181/package.zip")
        respx.get(SCENARIO_URL).mock(
            return_value=httpx.Response(
                200,
                json=_scenario(
                    "created",
                    estimate=None,
                    latest_run=_run(501, "error", status_detail=f"gate: {signed}"),
                ),
            )
        )
        _mock_boundary(True)
        data = _tool_json(await _build(_server))
        assert "X-Amz" not in json.dumps(data)
        assert data["outcome"] == "refused"
        assert data["status_detail"] == f"gate: {ELIDED}"
