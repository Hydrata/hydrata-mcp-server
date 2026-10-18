"""TASK-3812 (epic 3801 W1) — the MCP flood observation kind.

An agent creates a flood observation layer (waiting for its GeoServer layer),
adds, edits, lists and deletes observations through the REST routes hydrata
TASK-3807 built, and attaches a photo as a reference LINK only. MCP never
uploads photo files: a file (a local path, a data: URI, a file:// URL, or a
photo key on an observation) is refused with a message naming the UI path.

Proof: the 48 Towradgi 1998 flood marks (anuga_core
validation_tests/case_studies/towradgi/Validation/
historic_1998_flood_levels_towradgi_ck.csv, copied to tests/fixtures/) round-trip:
add_observations sends 48 POSTs, list_observations reads the same 48 back.
"""

import csv
import importlib
import json
from pathlib import Path

import httpx
import pytest
import respx

from tests.test_server import (
    BASE,
    BASE_MAP,
    MCP_HEADERS,
    _asgi_client,
    _call_as_caller,
    _mock_project,
    _parse,
    _rpc,
    _tool_error_text,
    _tool_json,
)

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "towradgi_1998_flood_levels.csv"
LAYERS = f"{BASE}/projects/42/flood-observations/"
LAYER = f"{LAYERS}239/"
OBSERVATIONS = f"{LAYER}observations/"
ALTERNATE = "geonode:obs_42_survey_1998"
OBSERVATION_TOOLS = {
    "create_observation_layer", "list_observations", "add_observations",
    "update_observation", "delete_observation", "add_observation_photo_link",
}


@pytest.fixture
def _server(env_vars):
    """The server module, reloaded after the env vars are set (as in test_server)."""
    import hydrata_mcp.server as srv
    importlib.reload(srv)
    return srv


def _layer(gn_layer=9876, **overrides):
    body = {"id": 239, "title": "Survey 1998", "project": 42, "gn_layer": gn_layer,
            "gn_layer_name": "obs_42_survey_1998" if gn_layer else None,
            "perms": ["view_resourcebase"], "vertical_datum": "AHD", "datum_offset_m": None,
            "has_features": False}
    body.update(overrides)
    return body


def _towradgi_marks():
    with FIXTURE.open() as handle:
        rows = list(csv.DictReader(handle))
    return [{
        "subtype": "flood_mark", "name": row["name"], "value_kind": "stage",
        "stage_m": float(row["elevation"]), "flood_name": "August 1998",
        "source": "Towradgi Creek 1998 flood levels (ANUGA validation)",
        "geometry": {"type": "Point", "coordinates": [float(row["easting"]), float(row["northing"])]},
    } for row in rows]


class FakeLayerTable:
    """The obs_ table behind the observation routes: POST assigns fids, GET lists them."""

    def __init__(self):
        self.features = {}
        self.bodies = []

    def read_shape(self, fid, body):
        return {"fid": fid, "layer": 239, "photo_count": 0, "problems": {}, **body}

    def create(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        fid = len(self.features) + 1
        self.features[fid] = body
        return httpx.Response(201, json=self.read_shape(fid, body))

    def listing(self, request):
        results = [self.read_shape(fid, body) for fid, body in self.features.items()]
        return httpx.Response(200, json={"layer": 239, "count": len(results), "results": results})


def _mock_table(table):
    respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer()))
    respx.post(OBSERVATIONS).mock(side_effect=table.create)
    respx.get(OBSERVATIONS).mock(side_effect=table.listing)
    _mock_project()


class TestObservationLayer:
    @respx.mock
    async def test_create_observation_layer_waits_for_its_geoserver_layer(self, _server):
        _mock_project()
        post = respx.post(LAYERS).mock(return_value=httpx.Response(201, json=_layer(gn_layer=None)))
        detail = respx.get(LAYER).mock(side_effect=[
            httpx.Response(200, json=_layer(gn_layer=None)),
            httpx.Response(200, json=_layer()),
        ])
        data = _tool_json(await _call_as_caller(_server, "create_observation_layer", {
            "project_id": 42, "title": "Survey 1998", "vertical_datum": "AHD",
            "poll_interval_seconds": 0}))
        assert json.loads(post.calls[0].request.content) == {
            "title": "Survey 1998", "vertical_datum": "AHD", "datum_offset_m": None}
        assert detail.call_count == 2
        assert data["layer_id"] == 239 and data["ready"] is True and data["timed_out"] is False
        assert data["dataset_alternate"] == ALTERNATE and data["vertical_datum"] == "AHD"
        assert f"/catalogue/#/map/{BASE_MAP}" in data["ui_url"] and f"layer={ALTERNATE}" in data["ui_url"]
        assert "Flood observation layer" in data["say_to_user"]
        assert "footprint" not in data["say_to_user"].lower()

    @respx.mock
    async def test_create_observation_layer_reports_not_ready_after_its_bound(self, _server):
        _mock_project()
        respx.post(LAYERS).mock(return_value=httpx.Response(201, json=_layer(gn_layer=None)))
        respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer(gn_layer=None)))
        data = _tool_json(await _call_as_caller(_server, "create_observation_layer", {
            "project_id": 42, "title": "Survey 1998", "timeout_seconds": 0}))
        assert data["ready"] is False and data["timed_out"] is True
        assert "list_observations" in data["say_to_user"]

    @respx.mock
    async def test_create_observation_layer_keeps_its_id_when_the_poll_fails(self, _server):
        _mock_project()
        respx.post(LAYERS).mock(return_value=httpx.Response(201, json=_layer(gn_layer=None)))
        respx.get(LAYER).mock(return_value=httpx.Response(503))
        data = _tool_json(await _call_as_caller(_server, "create_observation_layer", {
            "project_id": 42, "title": "Survey 1998", "poll_interval_seconds": 0}))
        assert data["layer_id"] == 239 and data["ready"] is False
        assert "server error" in data["poll_error"]

    @respx.mock
    async def test_create_observation_layer_refuses_a_local_datum_without_offset(self, _server):
        post = respx.post(LAYERS).mock(return_value=httpx.Response(201, json=_layer()))
        text = _tool_error_text(await _call_as_caller(_server, "create_observation_layer", {
            "project_id": 42, "title": "Gauge board", "vertical_datum": "local"}))
        assert "datum_offset_m" in text and not post.called

    @respx.mock
    async def test_any_datum_in_the_world_goes_to_the_api(self, _server):
        """TASK-3850 (operator grill Q5): no national list in the tool. An EPSG
        vertical CRS or a datum's own name is posted; the API validates it."""
        _mock_project()
        post = respx.post(LAYERS).mock(return_value=httpx.Response(201, json=_layer(vertical_datum="NAVD88")))
        respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer(vertical_datum="NAVD88")))
        data = _tool_json(await _call_as_caller(_server, "create_observation_layer", {
            "project_id": 42, "title": "Survey 2011", "vertical_datum": "NAVD88", "poll_interval_seconds": 0}))
        assert json.loads(post.calls[0].request.content)["vertical_datum"] == "NAVD88"
        assert data["vertical_datum"] == "NAVD88"
        assert "NAVD88" in data["say_to_user"]

    @respx.mock
    async def test_negative_control_the_api_refusal_of_a_non_vertical_code_is_relayed(self, _server):
        refusal = {"code": "invalid", "errors": ["vertical_datum: EPSG:4326 is not a vertical CRS; ..."],
                   "success": False}
        respx.post(LAYERS).mock(return_value=httpx.Response(400, json=refusal))
        text = await _call_as_caller(_server, "create_observation_layer", {
            "project_id": 42, "title": "x", "vertical_datum": "EPSG:4326"})
        assert "is not a vertical CRS" in json.dumps(text)

    async def test_the_datum_description_names_no_national_list(self, _server):
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        described = json.dumps(tools["create_observation_layer"]["inputSchema"])
        assert "EPSG vertical CRS" in described
        assert "AHD" not in described


class TestTowradgiRoundTrip:
    @respx.mock
    async def test_observation_48_towradgi_marks_round_trip(self, _server):
        marks = _towradgi_marks()
        assert len(marks) == 48
        table = FakeLayerTable()
        _mock_table(table)
        added = _tool_json(await _call_as_caller(_server, "add_observations", {
            "project_id": 42, "layer_id": 239, "observations": marks, "srid": 28356}))
        assert added["created_count"] == 48 and added["rejected_count"] == 0
        assert [row["fid"] for row in added["created"]] == list(range(1, 49))
        assert added["created"][0]["name"] == "PioneerRd"
        # srid fills only geometries that carry none; each body is otherwise the agent's own.
        assert all(body["geometry"]["srid"] == 28356 for body in table.bodies)
        assert f"layer={ALTERNATE}" in added["ui_url"]
        assert "48" in added["say_to_user"]

        listed = _tool_json(await _call_as_caller(_server, "list_observations", {
            "project_id": 42, "layer_id": 239}))
        assert listed["count"] == 48
        by_name = {row["name"]: row for row in listed["results"]}
        for mark in marks:
            back = by_name[mark["name"]]
            assert back["subtype"] == "flood_mark" and back["stage_m"] == mark["stage_m"]
            assert back["geometry"]["coordinates"] == mark["geometry"]["coordinates"]

    @respx.mock
    async def test_observation_rejected_rows_are_named_and_the_rest_land(self, _server):
        table = FakeLayerTable()
        _mock_table(table)
        problems = {"error_code": "VALIDATION_ERROR", "problems": {"stage_m": "required for a Flood mark"}}

        def create(request):
            if json.loads(request.content).get("name") == "bad":
                return httpx.Response(400, json=problems)
            return table.create(request)
        respx.post(OBSERVATIONS).mock(side_effect=create)
        marks = _towradgi_marks()[:2] + [{"subtype": "flood_mark", "name": "bad", "value_kind": "stage",
                                          "geometry": {"type": "Point", "coordinates": [150.9, -34.4]}}]
        data = _tool_json(await _call_as_caller(_server, "add_observations", {
            "project_id": 42, "layer_id": 239, "observations": marks}))
        assert data["created_count"] == 2 and data["rejected_count"] == 1
        assert data["rejected"] == [{"index": 2, "name": "bad", "http_status": 400,
                                     "problems": {"stage_m": "required for a Flood mark"}}]
        assert "srid" not in table.bodies[0]["geometry"]  # no srid given: the API's EPSG:4326 default

    @respx.mock
    async def test_an_undated_unnamed_mark_is_refused_and_the_agent_asks_which_flood(self, _server):
        """TASK-3847: a Flood mark / Dry point needs a date window or a flood
        name; the API's 400 names both fields and the agent is told to ask the
        user which flood (never guess a date)."""
        table = FakeLayerTable()
        _mock_table(table)
        message = ("Say which flood this is: give the date window (observed_start / observed_end) "
                   "or the flood name (flood_name).")
        problems = {"error_code": "VALIDATION_ERROR",
                    "problems": {"observed_start": message, "flood_name": message}}

        def create(request):
            body = json.loads(request.content)
            if not (body.get("flood_name") or body.get("observed_start") or body.get("observed_end")):
                return httpx.Response(400, json=problems)
            return table.create(request)
        respx.post(OBSERVATIONS).mock(side_effect=create)
        undated = {k: v for k, v in _towradgi_marks()[0].items() if k != "flood_name"}
        data = _tool_json(await _call_as_caller(_server, "add_observations", {
            "project_id": 42, "layer_id": 239, "observations": [_towradgi_marks()[1], undated]}))
        assert data["created_count"] == 1 and data["rejected_count"] == 1
        assert set(data["rejected"][0]["problems"]) == {"observed_start", "flood_name"}
        assert "which flood" in data["say_to_user"] and "ask the user" in data["say_to_user"]

    async def test_add_and_update_descriptions_say_a_date_or_flood_name_is_needed(self, _server):
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        for name in ("add_observations", "update_observation"):
            text = json.dumps(tools[name])
            assert "flood_name" in text and "date window" in text, name

    @respx.mock
    async def test_observation_add_refuses_a_layer_still_being_created(self, _server):
        respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer(gn_layer=None)))
        post = respx.post(OBSERVATIONS).mock(return_value=httpx.Response(201, json={}))
        text = _tool_error_text(await _call_as_caller(_server, "add_observations", {
            "project_id": 42, "layer_id": 239, "observations": _towradgi_marks()[:1]}))
        assert "still being created" in text and not post.called

    @respx.mock
    async def test_observation_add_stops_on_a_refusal_that_is_not_about_the_row(self, _server):
        table = FakeLayerTable()
        _mock_table(table)
        calls = []

        def create(request):
            calls.append(1)
            if len(calls) == 2:
                return httpx.Response(403, json={"detail": "You do not have permission"})
            return table.create(request)
        respx.post(OBSERVATIONS).mock(side_effect=create)
        data = _tool_json(await _call_as_caller(_server, "add_observations", {
            "project_id": 42, "layer_id": 239, "observations": _towradgi_marks()[:5]}))
        assert len(calls) == 2  # stopped: the other 3 would only fail the same way
        assert data["created_count"] == 1 and data["stopped"]["http_status"] == 403
        assert data["not_sent_count"] == 3
        assert "permission" in data["stopped"]["body"]["detail"]

    @respx.mock
    async def test_observation_add_keeps_what_landed_when_the_api_fails_mid_batch(self, _server):
        table = FakeLayerTable()
        _mock_table(table)
        calls = []

        def create(request):
            calls.append(1)
            if len(calls) == 3:
                return httpx.Response(502, text="<html>Bad Gateway</html>")
            return table.create(request)
        respx.post(OBSERVATIONS).mock(side_effect=create)
        data = _tool_json(await _call_as_caller(_server, "add_observations", {
            "project_id": 42, "layer_id": 239, "observations": _towradgi_marks()[:4]}))
        assert [row["fid"] for row in data["created"]] == [1, 2]
        assert data["stopped"]["index"] == 2 and "server error" in data["stopped"]["error"]
        assert data["not_sent_count"] == 1 and "were not added" in data["say_to_user"]

    @respx.mock
    async def test_list_observations_without_layer_lists_the_project_layers(self, _server):
        respx.get(LAYERS).mock(return_value=httpx.Response(200, json=[_layer(), _layer(gn_layer=None, id=240)]))
        data = _tool_json(await _call_as_caller(_server, "list_observations", {"project_id": 42}))
        assert [row["layer_id"] for row in data["layers"]] == [239, 240]
        assert data["layers"][0]["dataset_alternate"] == ALTERNATE
        assert data["layers"][1]["dataset_alternate"] is None


class TestEditDelete:
    @respx.mock
    async def test_update_observation_patches_only_the_changes(self, _server):
        _mock_project()
        respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer()))
        patch = respx.patch(f"{OBSERVATIONS}17/").mock(return_value=httpx.Response(200, json={
            "fid": 17, "layer": 239, "name": "1232", "stage_m": 2.5, "problems": {}}))
        data = _tool_json(await _call_as_caller(_server, "update_observation", {
            "project_id": 42, "layer_id": 239, "fid": 17, "changes": {"stage_m": 2.5}}))
        assert json.loads(patch.calls[0].request.content) == {"stage_m": 2.5}
        assert data["observation"]["stage_m"] == 2.5 and data["http_status"] == 200
        assert f"layer={ALTERNATE}" in data["ui_url"]

    @respx.mock
    async def test_update_observation_relays_the_api_problems(self, _server):
        respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer()))
        respx.patch(f"{OBSERVATIONS}17/").mock(return_value=httpx.Response(400, json={
            "error_code": "VALIDATION_ERROR", "problems": {"bound": "must be one of: exact, at_least, at_most"}}))
        text = _tool_error_text(await _call_as_caller(_server, "update_observation", {
            "project_id": 42, "layer_id": 239, "fid": 17, "changes": {"bound": "about"}}))
        assert "must be one of: exact, at_least, at_most" in text

    @respx.mock
    async def test_update_observation_refuses_empty_changes(self, _server):
        text = _tool_error_text(await _call_as_caller(_server, "update_observation", {
            "project_id": 42, "layer_id": 239, "fid": 17, "changes": {}}))
        assert "changes" in text

    @respx.mock
    async def test_delete_observation_deletes_one_feature(self, _server):
        _mock_project()
        respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer()))
        delete = respx.delete(f"{OBSERVATIONS}17/").mock(return_value=httpx.Response(204))
        data = _tool_json(await _call_as_caller(_server, "delete_observation", {
            "project_id": 42, "layer_id": 239, "fid": 17}))
        assert delete.called and data["deleted"] is True and data["fid"] == 17
        assert "photos" in data["say_to_user"]

    @respx.mock
    async def test_delete_observation_404_is_an_error(self, _server):
        respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer()))
        respx.delete(f"{OBSERVATIONS}99/").mock(return_value=httpx.Response(404, json={"detail": "Not found."}))
        text = _tool_error_text(await _call_as_caller(_server, "delete_observation", {
            "project_id": 42, "layer_id": 239, "fid": 99}))
        assert "404" in text


class TestPhotoLinkOnly:
    @respx.mock
    async def test_photo_link_is_stored_as_reference_only(self, _server):
        _mock_project()
        respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer()))
        post = respx.post(f"{OBSERVATIONS}1/photos/").mock(return_value=httpx.Response(201, json={
            "id": 12, "layer": 239, "fid": 1, "source": "reference_only", "source_label": "Reference link",
            "reference_url": "https://example.org/towradgi-1998.jpg", "display_url": None, "url": None,
            "caption": "Debris line, Pioneer Rd", "public_consent": False}))
        data = _tool_json(await _call_as_caller(_server, "add_observation_photo_link", {
            "project_id": 42, "layer_id": 239, "fid": 1,
            "reference_url": "https://example.org/towradgi-1998.jpg", "caption": "Debris line, Pioneer Rd"}))
        assert json.loads(post.calls[0].request.content) == {
            "source": "reference_only", "reference_url": "https://example.org/towradgi-1998.jpg",
            "caption": "Debris line, Pioneer Rd", "credit": "", "public_consent": False}
        assert data["photo"]["id"] == 12 and data["http_status"] == 201

    @pytest.mark.parametrize("reference", [
        "/home/engineer/photos/IMG_0042.jpg",
        "file:///home/engineer/photos/IMG_0042.HEIC",
        "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ==",
        "C:\\Users\\engineer\\IMG_0042.jpg",
    ])
    @respx.mock(assert_all_called=False)
    async def test_observation_photo_file_upload_is_refused_naming_the_ui_path(self, _server, reference):
        """NEGATIVE CONTROL: MCP never uploads a photo file; the refusal names where to do it."""
        post = respx.post(url__startswith=f"{OBSERVATIONS}").mock(return_value=httpx.Response(201, json={}))
        text = _tool_error_text(await _call_as_caller(_server, "add_observation_photo_link", {
            "project_id": 42, "layer_id": 239, "fid": 1, "reference_url": reference}))
        assert "photo strip" in text and "Observations" in text and "https://" in text
        assert not post.called

    @respx.mock(assert_all_called=False)
    async def test_observation_photo_key_on_an_observation_is_refused_naming_the_ui_path(self, _server):
        post = respx.post(OBSERVATIONS).mock(return_value=httpx.Response(201, json={}))
        respx.get(LAYER).mock(return_value=httpx.Response(200, json=_layer()))
        mark = dict(_towradgi_marks()[0], photo="IMG_0042.jpg")
        text = _tool_error_text(await _call_as_caller(_server, "add_observations", {
            "project_id": 42, "layer_id": 239, "observations": [mark]}))
        assert "photo strip" in text and "add_observation_photo_link" in text
        assert not post.called


class TestObservationToolSurface:
    async def test_observation_tools_are_listed_with_their_hints(self, _server):
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=MCP_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        assert OBSERVATION_TOOLS <= set(tools)
        assert tools["list_observations"]["annotations"]["readOnlyHint"] is True
        assert tools["delete_observation"]["annotations"]["destructiveHint"] is True
        assert tools["add_observations"]["annotations"]["destructiveHint"] is False
        for name in OBSERVATION_TOOLS:
            description = tools[name]["description"]
            assert "footprint" not in description.lower() and "calibration target" not in description.lower()
        assert "Flood mark" in tools["add_observations"]["description"]
        assert "photo strip" in tools["add_observation_photo_link"]["description"]
