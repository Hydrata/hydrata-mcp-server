"""Input tools: time series, attach_input_layer, list_inputs and list_time_series."""

import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    CALLER_KEY,
    EXEC_ID,
    EXEC_STATUS_URL,
    KEY_HEADERS,
    ORIGIN,
    ROW_DATA,
    SUMMARY_HYDROGRAPH,
    _asgi_client,
    _call_as_caller,
    _dataset,
    _exec,
    _gs_tripwire,
    _parse,
    _row,
    _rpc,
    _series_args,
    _series_row,
    _tool_error_text,
    _tool_json,
)

pytestmark = pytest.mark.asyncio


EXEC_REQUEST_URL = f"{ORIGIN}/api/v2/executionrequest/{EXEC_ID}/"
INPUT_LAYER_ROUTES = {
    "boundary": "boundaries",
    "friction": "frictions",
    "inflow": "inflows",
    "rainfall": "rainfalls",
    "building": "buildings",
    "mesh_region": "mesh-regions",
}
DEFAULT_TITLES = {
    "boundary": "Boundary 01",
    "friction": "Friction 01",
    "inflow": "Inflow 01",
    "rainfall": "Rainfall 01",
    "building": "Building 01",
    "mesh_region": "MeshRegion 01",
}


def _mock_no_wrapper_posts():
    """The two routes attach_input_layer must NEVER hit (see the block comment)."""
    return (
        respx.post(f"{BASE}/projects/42/buildings/").mock(return_value=httpx.Response(201, json={})),
        respx.post(f"{BASE}/projects/42/mesh-regions/").mock(return_value=httpx.Response(201, json={})),
    )


class TestCreateTimeSeriesTool:
    @respx.mock
    async def test_create_time_series_posts_series_type_and_units_top_level_and_forwards_the_key(self, _server):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        result = await _call_as_caller(_server, "create_time_series", _series_args())
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        body = json.loads(route.calls[0].request.content)
        # AC (flipped at the re-aim): both are TOP-LEVEL body keys, never folded
        # into description; rows travel VERBATIM (clean() normalises them server-side).
        assert body == {
            "name": "rain_gauge_200",
            "description": "",
            "source": "",
            "location_name": "",
            "timezone": "Australia/Sydney",
            "series_type": "hyetograph",
            "units": "mm/hr",
            "hold_last_value": False,
            "data": {"rowData": ROW_DATA},
        }
        assert "series_type" not in body["description"]
        data = _tool_json(result)
        # Compact record: the API echoes the whole row incl. `data` (~100 KB for
        # a 1447-row gauge); 50 of those would drown the driving agent's context.
        compact = {
            "id": 77,
            "name": "rain_gauge_200",
            "series_type": "hyetograph",
            "units": "mm/hr",
            "timezone": "Australia/Sydney",
            "row_count": 3,
            "http_status": 201,
        }
        # TASK-3203 adds only the hand-back keys on top of the compact record.
        assert {k: data[k] for k in compact} == compact
        assert set(data) - set(compact) <= {"ui_url", "summary", "say_to_user", "hand_back_error", "hold_last_value"}
        assert "data" not in data

    @respx.mock
    async def test_create_time_series_sends_stage_series_type_verbatim(self, _server):
        """The tide series in the bundle is `stage`; a build that drops the key
        would still land as the model default (hyetograph) and grade green."""
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(78, series_type="stage", units="m"))
        )
        data = _tool_json(
            await _call_as_caller(_server, "create_time_series", _series_args(series_type="stage", units="m"))
        )
        body = json.loads(route.calls[0].request.content)
        assert body["series_type"] == "stage"
        assert body["units"] == "m"
        assert data["series_type"] == "stage"

    @respx.mock
    async def test_create_time_series_rejects_unknown_series_type_without_upstream_call(self, _server):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        text = _tool_error_text(
            await _call_as_caller(_server, "create_time_series", _series_args(series_type="rainfall"))
        )
        assert not route.called
        for choice in ("hyetograph", "hydrograph", "stage", "generic"):
            assert choice in text

    @pytest.mark.parametrize(
        "bad_data",
        [
            [{"timestamp": "1998-08-17T00:00:00", "value": 1}],  # a bare list (model default!)
            {"rows": []},  # no rowData
            {"rowData": {"timestamp": "1998-08-17T00:00:00", "value": 1}},  # rowData not a list
            "not even json",
        ],
        ids=["list", "no-rowData", "rowData-not-list", "string"],
    )
    @respx.mock
    async def test_create_time_series_rejects_non_rowdata_shape_without_upstream_call(self, _server, bad_data):
        """A non-{rowData: [...]} `data` is an UNHANDLED 500 on the API (clean()
        does data.get('rowData') without a guard) whose body the client masks."""
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        text = _tool_error_text(await _call_as_caller(_server, "create_time_series", _series_args(data=bad_data)))
        assert not route.called
        # A non-object is refused one layer earlier, by the tool's own input
        # schema (`data: dict` → pydantic "Input should be a valid dictionary");
        # an object of the wrong shape by _validate_row_data. Both: no request.
        assert "rowData" in text or "valid dictionary" in text

    @pytest.mark.parametrize(
        "bad_row",
        [
            {"timestamp": "17/08/1998 00:00", "value": 1.0},  # not ISO 8601
            {"timestamp": "1998-08-17T00:00:00", "value": "heavy"},  # not a number
            {"timestamp": "1998-08-17T00:00:00"},  # value missing
            {"value": 1.0},  # timestamp missing
            {"timestamp": 19980817, "value": 1.0},  # timestamp not a string
            ["1998-08-17T00:00:00", 1.0],  # row not a dict
        ],
        ids=["bad-iso", "non-numeric", "no-value", "no-timestamp", "int-timestamp", "row-list"],
    )
    @respx.mock
    async def test_create_time_series_rejects_bad_row_without_upstream_call(self, _server, bad_row):
        """Mirrors TimeSeries.clean() (hydrology/models.py:126-151): its
        ValidationError is raised from full_clean() inside save(), which DRF
        does NOT map to a 400 — the API answers 500 and the client hides the
        body. The tool must refuse first, naming the offending row."""
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        text = _tool_error_text(
            await _call_as_caller(
                _server,
                "create_time_series",
                _series_args(data={"rowData": [ROW_DATA[0], bad_row]}),
            )
        )
        assert not route.called
        assert "row 1" in text

    @respx.mock
    async def test_create_time_series_400_surfaces_the_api_detail(self, _server):
        """A bad `timezone` IS a 400 (DRF ChoiceField) — its detail must reach the agent."""
        from hydrata_mcp.client import HydrataAPIError

        respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(400, json={"timezone": ['"Mars/Olympus" is not a valid choice.']})
        )
        with pytest.raises(HydrataAPIError, match="400") as exc_info:
            await _server.create_time_series(**{k: v for k, v in _series_args(timezone="Mars/Olympus").items()})
        assert "Mars/Olympus" in str(exc_info.value)


HYDROGRAPH_ROWS = {
    "rowData": [
        {"timestamp": f"2020-01-01T0{h}:00:00", "value": v} for h, v in ((0, 0.0), (2, 19.7), (4, 5.0), (6, 0.0))
    ]
}


class TestCreateTimeSeriesUnitsAndSummary:
    @pytest.mark.parametrize(
        "series_type,units",
        [
            ("hyetograph", "m3/s"),
            ("hydrograph", "mm/hr"),
            ("stage", "mm"),
            ("hyetograph", ""),
            ("hydrograph", ""),
        ],
    )
    @respx.mock
    async def test_create_time_series_units_rule_refuses_before_any_post(self, _server, series_type, units):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(201, json={}))
        text = _tool_error_text(
            await _call_as_caller(_server, "create_time_series", _series_args(series_type=series_type, units=units))
        )
        assert not route.called
        assert (
            "engine reads" in text
            and "Nothing was sent" in text
            and f"units={_server.SERIES_UNITS[series_type][0]!r}" in text
        )

    @pytest.mark.parametrize(
        "series_type,units",
        [
            ("hyetograph", "mm/h"),
            ("hydrograph", "m^3/s"),
            ("hydrograph", "cumec"),
            ("stage", "m"),
            ("generic", ""),
            ("generic", "anything"),
        ],
    )
    @respx.mock
    async def test_create_time_series_units_rule_lets_matching_units_through(self, _server, series_type, units):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77, series_type=series_type, units=units))
        )
        await _call_as_caller(_server, "create_time_series", _series_args(series_type=series_type, units=units))
        assert route.called

    @respx.mock
    async def test_create_time_series_hold_last_value_is_forwarded(self, _server):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77, hold_last_value=True))
        )
        data = _tool_json(await _call_as_caller(_server, "create_time_series", _series_args(hold_last_value=True)))
        assert json.loads(route.calls[0].request.content)["hold_last_value"] is True
        assert data["hold_last_value"] is True

    @respx.mock
    async def test_create_time_series_hold_last_value_trailing_refusal_reaches_the_agent(self, _server):
        """The trailing-value rule lives on the server: its 400 body (error_code + hint) must be relayed."""
        from hydrata_mcp.client import HydrataAPIError

        respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error_code": "TRAILING_VALUE_HELD",
                    "hint": "End the series with a 0 row, or send hold_last_value",
                },
            )
        )
        with pytest.raises(HydrataAPIError, match="TRAILING_VALUE_HELD"):
            await _server.create_time_series(**_series_args(hold_last_value=False))

    @respx.mock
    async def test_create_time_series_echoes_the_server_summary_and_says_it(self, _server):
        respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(
                201,
                json=_series_row(
                    77,
                    name="Creek in",
                    series_type="hydrograph",
                    units="m3/s",
                    timezone="UTC",
                    summary=SUMMARY_HYDROGRAPH,
                    data=HYDROGRAPH_ROWS,
                ),
            )
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "create_time_series",
                _series_args(
                    name="Creek in", series_type="hydrograph", units="m3/s", timezone="UTC", data=HYDROGRAPH_ROWS
                ),
            )
        )
        assert data["series_summary"] == SUMMARY_HYDROGRAPH
        assert data["say_to_user"].startswith(
            "Hydrograph Creek in: peak 19.7 m3/s at 02:00, volume 1.2e+05 m3, ends at 0."
        )

    @respx.mock
    async def test_create_time_series_without_a_server_summary_is_unchanged(self, _server):
        respx.post(f"{BASE}/projects/42/time-series/").mock(return_value=httpx.Response(201, json=_series_row(77)))
        data = _tool_json(await _call_as_caller(_server, "create_time_series", _series_args()))
        assert "series_summary" not in data and data["say_to_user"].startswith("Time series 'rain_gauge_200'")


class TestAttachInputLayerTool:
    @respx.mock
    async def test_attach_input_layer_legacy_structure_kind_patches_buildings_with_a_deprecation_note(self, _server):
        """TASK-3586: kind 'structure' (the pre-rename name) still attaches — to the
        BUILDINGS route — and the result carries a deprecation note; kind
        'building' is the same call without the note."""
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        list_route = respx.get(f"{BASE}/projects/42/buildings/").mock(
            return_value=httpx.Response(200, json=[_row(7, "Building 01", gn_layer=1400)])
        )
        patch_route = respx.patch(f"{BASE}/projects/42/buildings/7/").mock(
            return_value=httpx.Response(200, json=_row(7, "Building 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:str_42_building_01"))
        )
        legacy_route = respx.get(f"{BASE}/projects/42/structures/").mock(return_value=httpx.Response(200, json=[]))
        args = {"project_id": 42, "execution_id": EXEC_ID, "poll_interval_seconds": 0}

        legacy = _tool_json(await _call_as_caller(_server, "attach_input_layer", {**args, "kind": "structure"}))
        assert legacy["outcome"] == "attached"
        assert legacy["kind"] == "building" and legacy["route"] == "buildings"
        assert "deprecated" in legacy["deprecation"] and "building" in legacy["deprecation"]
        assert json.loads(patch_route.calls[0].request.content) == {"gn_layer": 1502}
        assert not legacy_route.called  # the old plural route is never hit

        current = _tool_json(await _call_as_caller(_server, "attach_input_layer", {**args, "kind": "building"}))
        assert current["outcome"] == "attached" and current["route"] == "buildings"
        assert "deprecation" not in current
        assert list_route.call_count == 2 and patch_route.call_count == 2

    @pytest.mark.parametrize("kind", list(INPUT_LAYER_ROUTES))
    @respx.mock
    async def test_attach_input_layer_patches_the_default_row_and_forwards_the_key(self, _server, kind):
        route_name = INPUT_LAYER_ROUTES[kind]
        pk = 124_556
        # The execution-status route is GeoNode's, at the ORIGIN — not under /api/v2/anuga.
        status_route = respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}]))
        )
        list_route = respx.get(f"{BASE}/projects/42/{route_name}/").mock(
            return_value=httpx.Response(200, json=[_row(pk, DEFAULT_TITLES[kind], gn_layer=1400)])
        )
        patch_route = respx.patch(f"{BASE}/projects/42/{route_name}/{pk}/").mock(
            return_value=httpx.Response(200, json=_row(pk, DEFAULT_TITLES[kind], gn_layer=1502))
        )
        dataset_route = respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:rai_42_rainfall_01"))
        )
        buildings_post, mesh_regions_post = _mock_no_wrapper_posts()

        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": kind, "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )

        for route in (status_route, list_route, patch_route, dataset_route):
            assert route.called, route
            assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert str(status_route.calls[0].request.url) == EXEC_STATUS_URL
        # The PATCH: plural route + the default row's pk, body exactly {"gn_layer": pk}.
        assert json.loads(patch_route.calls[0].request.content) == {"gn_layer": 1502}
        # NEVER a POST to the two wrapper routes (the async layer factory would
        # overwrite gn_layer moments after the 201).
        assert not buildings_post.called
        assert not mesh_regions_post.called
        assert data["outcome"] == "attached"
        assert data["kind"] == kind
        assert data["route"] == route_name
        assert data["row_id"] == pk
        assert data["row_title"] == DEFAULT_TITLES[kind]
        assert data["row_ids"] == [pk]
        assert data["gn_layer"] == 1502
        assert data["dataset_pk"] == 1502
        assert data["dataset_alternate"] == "geonode:rai_42_rainfall_01"
        assert data["execution_status"] == "finished"
        assert data["http_status"] == 200

    @pytest.mark.parametrize("kind", ["breakline", "culvert"])
    @respx.mock
    async def test_attach_input_layer_refuses_breakline_and_culvert_without_any_write(self, _server, kind):
        """Breakline is still refused before any request. TASK-3641 (epic 3585)
        INVERTED the culvert case: kind=culvert is no longer refused — it reaches
        the upload poll (a failed import here, so it still writes nothing). The
        name is kept: renaming a test counts as deleting it (ratchet)."""
        # TASK-3203: the refusal now reads the project once (unmocked here, so
        # its link is null) to hand the human a ui_url; it still writes nothing.
        status_route = respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("failed", log="bad file"))
        )
        list_routes = [
            respx.get(f"{BASE}/projects/42/{r}/").mock(return_value=httpx.Response(200, json=[]))
            for r in (*INPUT_LAYER_ROUTES.values(), "culverts")
        ]
        culverts_post = respx.post(f"{BASE}/projects/42/culverts/").mock(return_value=httpx.Response(201, json={}))
        buildings_post, mesh_regions_post = _mock_no_wrapper_posts()
        result = await _call_as_caller(
            _server, "attach_input_layer", {"project_id": 42, "kind": kind, "execution_id": EXEC_ID}
        )
        assert not any(r.called for r in list_routes)
        assert not buildings_post.called and not mesh_regions_post.called and not culverts_post.called
        if kind == "breakline":
            text = _tool_error_text(result)
            assert not status_route.called
            assert "TASK-3040" in text
            assert kind in text
        else:
            data = _tool_json(result)
            assert status_route.called
            assert data["outcome"] == "upload_failed" and data["kind"] == "culvert"
            assert data["route"] == "culverts"

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../../api/v2/users/",  # dot-segments: would reach another route at the origin
            "b7e1c2d3-4444-4555-8666-77778888999",  # one hex digit short
            f"{EXEC_ID}?format=json",  # query injection
            "",
        ],
        ids=["traversal", "short", "query", "empty"],
    )
    @respx.mock
    async def test_attach_input_layer_refuses_a_non_uuid_execution_id_without_any_http(self, _server, bad_id):
        """W1a sweep (epic 2467): execution_id is the ONE free-form path segment a
        tool interpolates into a URL, and on prod that URL is the unthrottled
        loopback door to Django (geonode-https.j2 :8081). GeoNode's exec_id is a
        UUIDField, so anything else is refused before a request is built."""
        catch_all = respx.route().mock(return_value=httpx.Response(200, json={}))
        text = _tool_error_text(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                # timeout 0 so an un-refused id fails fast as `timed_out`, not a hang
                {"project_id": 42, "kind": "rainfall", "execution_id": bad_id, "timeout_seconds": 0},
            )
        )
        assert not catch_all.called
        assert "UUID" in text

    @respx.mock
    async def test_attach_input_layer_refuses_unknown_kind_listing_the_six(self, _server):
        status_route = respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}]))
        )
        text = _tool_error_text(
            await _call_as_caller(
                _server, "attach_input_layer", {"project_id": 42, "kind": "terrain", "execution_id": EXEC_ID}
            )
        )
        assert not status_route.called
        for kind in INPUT_LAYER_ROUTES:
            assert kind in text

    @respx.mock
    async def test_attach_input_layer_surfaces_execution_failure_verbatim(self, _server):
        failed = _exec(
            "failed",
            log="Unable to import the file: rainfall.geojson is not a valid GeoJSON",
            step="import",
        )
        status_route = respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=failed))
        list_route = respx.get(f"{BASE}/projects/42/rainfalls/").mock(
            return_value=httpx.Response(200, json=[_row(1, "Rainfall 01")])
        )
        patch_route = respx.patch(f"{BASE}/projects/42/rainfalls/1/").mock(return_value=httpx.Response(200, json={}))
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "rainfall", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert status_route.call_count == 1  # failed is terminal: no further polls
        assert not list_route.called
        assert not patch_route.called
        assert data["outcome"] == "upload_failed"
        assert data["execution_status"] == "failed"
        assert data["execution"] == failed  # verbatim
        assert "not a valid GeoJSON" in data["execution"]["log"]

    @respx.mock
    async def test_attach_input_layer_falls_back_to_executionrequest_for_the_dataset_pk(self, _server):
        """finished but output_params.resources empty → GET /api/v2/executionrequest/<id>/
        → request.output_params.resources[0].id (the e2e recipe's fallback)."""
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [])))
        fallback = respx.get(EXEC_REQUEST_URL).mock(
            return_value=httpx.Response(200, json={"request": {"output_params": {"resources": [{"id": "1502"}]}}})
        )
        respx.get(f"{BASE}/projects/42/boundaries/").mock(
            return_value=httpx.Response(200, json=[_row(27497, "Boundary 01")])
        )
        patch_route = respx.patch(f"{BASE}/projects/42/boundaries/27497/").mock(
            return_value=httpx.Response(200, json=_row(27497, "Boundary 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:boundary"))
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "boundary", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert fallback.called
        assert fallback.calls[0].request.headers["authorization"] == CALLER_KEY
        assert json.loads(patch_route.calls[0].request.content) == {"gn_layer": 1502}
        assert data["outcome"] == "attached"
        assert data["dataset_pk"] == 1502

    @respx.mock
    async def test_attach_input_layer_polls_ready_running_then_finished(self, _server):
        status_route = respx.get(EXEC_STATUS_URL).mock(
            side_effect=[
                httpx.Response(200, json=_exec("ready")),
                httpx.Response(200, json=_exec("running")),
                httpx.Response(200, json=_exec("finished", [{"id": 1502}])),
            ]
        )
        respx.get(f"{BASE}/projects/42/frictions/").mock(
            return_value=httpx.Response(200, json=[_row(5, "Friction 01")])
        )
        respx.patch(f"{BASE}/projects/42/frictions/5/").mock(
            return_value=httpx.Response(200, json=_row(5, "Friction 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:friction"))
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "friction", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert status_route.call_count == 3
        assert data["polls"] == 3
        assert data["outcome"] == "attached"

    @respx.mock
    async def test_attach_input_layer_timeout_is_bounded_and_patches_nothing(self, _server):
        """timeout_seconds=0 → ONE status read, then `timed_out`; the agent re-calls."""
        status_route = respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("running")))
        list_route = respx.get(f"{BASE}/projects/42/rainfalls/").mock(
            return_value=httpx.Response(200, json=[_row(1, "Rainfall 01")])
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {
                    "project_id": 42,
                    "kind": "rainfall",
                    "execution_id": EXEC_ID,
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0,
                },
            )
        )
        assert status_route.call_count == 1
        assert not list_route.called
        assert data["outcome"] == "timed_out"
        assert data["execution_status"] == "running"
        assert data["polls"] == 1

    @respx.mock
    async def test_attach_input_layer_with_no_default_row_says_run_finalize_first(self, _server):
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        respx.get(f"{BASE}/projects/42/inflows/").mock(return_value=httpx.Response(200, json=[]))
        patch_route = respx.patch(url__regex=rf"{BASE}/projects/42/inflows/\d+/").mock(
            return_value=httpx.Response(200, json={})
        )
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "inflow", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert not patch_route.called
        assert data["outcome"] == "no_default_row"
        assert "finalize_terrain_upload" in data["message"]
        assert data["dataset_pk"] == 1502  # the upload DID land; only the row is missing

    @respx.mock
    async def test_attach_input_layer_with_several_rows_patches_the_01_row_and_reports_ids(self, _server):
        """'First' is deterministic: the '<Kind> 01' row if present, else the lowest id
        (the list has no declared ordering). Every id is reported."""
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        respx.get(f"{BASE}/projects/42/mesh-regions/").mock(
            return_value=httpx.Response(
                200,
                json=[_row(9, "MeshRegion 02"), _row(5, "MeshRegion 01"), _row(2, "Fine mesh")],
            )
        )
        patch_route = respx.patch(f"{BASE}/projects/42/mesh-regions/5/").mock(
            return_value=httpx.Response(200, json=_row(5, "MeshRegion 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:mesh"))
        )
        _mock_no_wrapper_posts()
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "mesh_region", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert patch_route.called
        assert data["row_id"] == 5
        assert data["row_ids"] == [9, 5, 2]

    @respx.mock
    async def test_attach_building_picks_a_pre_rename_structure_01_row(self, _server):
        """TASK-3586: an existing project's default building row is titled
        'Structure 01'. It is the default row even when a user-added building
        has a lower id."""
        respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
        respx.get(f"{BASE}/projects/42/buildings/").mock(
            return_value=httpx.Response(200, json=[_row(9, "Structure 01"), _row(3, "Garage")])
        )
        patch_route = respx.patch(f"{BASE}/projects/42/buildings/9/").mock(
            return_value=httpx.Response(200, json=_row(9, "Structure 01", gn_layer=1502))
        )
        respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, "geonode:str"))
        )
        _mock_no_wrapper_posts()
        data = _tool_json(
            await _call_as_caller(
                _server,
                "attach_input_layer",
                {"project_id": 42, "kind": "building", "execution_id": EXEC_ID, "poll_interval_seconds": 0},
            )
        )
        assert patch_route.called
        assert data["row_id"] == 9

    async def test_attach_input_layer_and_time_series_listed_culvert_conveyed_no_file_inputs(self, _server):
        """AC3: the culvert engine conveys flow (TASK-3661: no 'not conveyed' caveat) in attach_input_layer's description; neither
        tool takes file contents (create_time_series takes a small JSON `data`
        object by design — decision D7 forbids bytes, not row objects)."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        assert "create_time_series" in tools and "attach_input_layer" in tools
        attach = tools["attach_input_layer"]["description"]
        assert "not conveyed" not in attach
        assert "conveys flow through each culvert" in attach
        assert "TASK-3040" in attach
        assert "base_file" in attach  # the curl recipe the agent runs itself
        # TASK-4064: a shell-less connector agent is sent to the inline / link paths first.
        assert "write_input_layer" in attach and "request_input_upload" in attach
        for name in ("create_time_series", "attach_input_layer"):
            props = tools[name]["inputSchema"]["properties"]
            for forbidden in ("file", "bytes", "base64", "content", "path", "geojson"):
                assert forbidden not in props, (name, forbidden)
        series_props = tools["create_time_series"]["inputSchema"]["properties"]
        assert "series_type" in series_props and "units" in series_props


def _wrapper_rows(route):
    rows = {
        "boundaries": [
            {
                "id": 1,
                "title": "Boundary 01",
                "gn_layer": 11,
                "gn_layer_name": "bdy_42_boundary_01",
                "has_features": True,
                "has_external_feature": True,
            }
        ],
        # TASK-3603 (friction, building) + TASK-3487 (inflow): every wrapper
        # serializer now carries has_features, so list_inputs never asks /gs/.
        "frictions": [
            {
                "id": 2,
                "title": "Friction 01",
                "gn_layer": 12,
                "gn_layer_name": "fri_42_friction_01",
                "has_features": True,
            }
        ],
        "inflows": [{"id": 3, "title": "Inflow 01", "gn_layer": None, "gn_layer_name": None, "has_features": False}],
        "rainfalls": [
            {
                "id": 4,
                "title": "Rainfall 01",
                "gn_layer": 14,
                "gn_layer_name": "rai_42_rainfall_01",
                "has_features": False,
            }
        ],
        "buildings": [
            {
                "id": 5,
                "title": "Building 01",
                "gn_layer": 15,
                "gn_layer_name": "str_42_building_01",
                "has_features": False,
            }
        ],
        "mesh-regions": [
            {
                "id": 6,
                "title": "MeshRegion 01",
                "gn_layer": 16,
                "gn_layer_name": "mes_42_meshregion_01",
                "has_features": True,
            }
        ],
        # TASK-3641: CulvertSerializerV2 carries has_features (no WFS lookup).
        "culverts": [
            {"id": 7, "title": "Culvert 01", "gn_layer": 17, "gn_layer_name": "cul_42_culvert_01", "has_features": True}
        ],
    }
    return rows[route]


class TestListInputs:
    @respx.mock
    async def test_list_inputs_every_kind_with_alternate_and_has_features(self, _server, monkeypatch):
        """TASK-3749: every kind's has_features is the API's; no /gs/ request at all."""
        gs = _gs_tripwire(_server, monkeypatch)
        for route in (*INPUT_LAYER_ROUTES.values(), "culverts"):
            respx.get(f"{BASE}/projects/42/{route}/").mock(return_value=httpx.Response(200, json=_wrapper_rows(route)))
        data = json.loads(await _server.list_inputs(project_id=42))
        inputs = data["inputs"]
        assert gs == []
        assert set(inputs) == {*INPUT_LAYER_ROUTES, "culvert"}  # TASK-3641
        assert inputs["culvert"][0] == {
            "row_id": 7,
            "title": "Culvert 01",
            "gn_layer": 17,
            "dataset_alternate": "geonode:cul_42_culvert_01",
            "has_features": True,
        }
        assert inputs["boundary"][0] == {
            "row_id": 1,
            "title": "Boundary 01",
            "gn_layer": 11,
            "dataset_alternate": "geonode:bdy_42_boundary_01",
            "has_features": True,
            "has_external_feature": True,
        }
        assert inputs["friction"][0] == {
            "row_id": 2,
            "title": "Friction 01",
            "gn_layer": 12,
            "dataset_alternate": "geonode:fri_42_friction_01",
            "has_features": True,
        }
        assert inputs["building"][0]["has_features"] is False
        assert inputs["inflow"][0] == {
            "row_id": 3,
            "title": "Inflow 01",
            "gn_layer": None,
            "dataset_alternate": None,
            "has_features": False,
        }
        assert inputs["rainfall"][0]["has_features"] is False
        assert all("feature_count" not in row for rows in inputs.values() for row in rows)

    @respx.mock
    async def test_list_inputs_row_without_has_features_is_null_without_a_gs_lookup(self, _server, monkeypatch):
        """TASK-3749 AC1: a row whose record lacks has_features (an older API) is null, unknown —
        never a /gs/ WFS count, which Hydrata refuses for every agent key (TASK-3728)."""
        gs = _gs_tripwire(_server, monkeypatch)

        def stale(route):
            rows = _wrapper_rows(route)
            if route in ("frictions", "buildings"):
                rows = [{k: v for k, v in row.items() if k != "has_features"} for row in rows]
            return rows

        for route in (*INPUT_LAYER_ROUTES.values(), "culverts"):
            respx.get(f"{BASE}/projects/42/{route}/").mock(return_value=httpx.Response(200, json=stale(route)))
        data = json.loads(await _server.list_inputs(project_id=42))
        inputs = data["inputs"]
        assert gs == []
        for kind in ("friction", "building"):
            assert inputs[kind][0]["has_features"] is None  # unknown, not false and not an error
            assert "feature_count" not in inputs[kind][0]
        assert inputs["boundary"][0]["has_features"] is True  # API-carried kinds are unaffected
        assert inputs["inflow"][0]["has_features"] is False  # no layer: still false
        assert "error" not in data

    @respx.mock
    async def test_gs_tripwire_negative_control_catches_a_request_the_client_makes(self, _server, monkeypatch):
        """Negative control for the two tests above: the tripwire really matches the /gs/ URL
        the MCP's own client builds, so `call_count == 0` there is not vacuous."""
        gs = _gs_tripwire(_server, monkeypatch)
        with pytest.raises(AssertionError, match="TASK-3749"):
            await _server.client.get_from_origin("/gs/ows", params={"service": "WFS"})
        assert len(gs) == 1


class TestListTimeSeries:
    @respx.mock
    async def test_list_time_series_follows_pages_and_drops_the_rows(self, _server):
        page1 = {"count": 2, "next": "…?page=2", "previous": None, "results": [_series_row(77)]}
        page2 = {
            "count": 2,
            "next": None,
            "previous": "…",
            "results": [_series_row(78, name="tide", series_type="stage", units="m", data={"rowData": []})],
        }
        route = respx.get(f"{BASE}/projects/42/time-series/").mock(
            side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
        )
        data = json.loads(await _server.list_time_series(project_id=42))
        assert route.call_count == 2
        assert route.calls[1].request.url.params["page"] == "2"
        assert data["count"] == 2 and "truncated" not in data
        first, tide = data["series"]
        assert first == {
            "id": 77,
            "name": "rain_gauge_200",
            "series_type": "hyetograph",
            "units": "mm/hr",
            "timezone": "Australia/Sydney",
            "row_count": 3,
            "first_timestamp": ROW_DATA[0]["timestamp"],
            "last_timestamp": ROW_DATA[-1]["timestamp"],
        }
        assert tide["row_count"] == 0 and tide["first_timestamp"] is None
        assert "rowData" not in json.dumps(data)
