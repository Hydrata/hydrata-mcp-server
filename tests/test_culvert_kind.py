"""TASK-3641 (epic 3585 W2b) — attach_input_layer kind=culvert.

The culvert refusal is lifted: the tool reads the agent's uploaded culverts
back over WFS, maps their attribute names onto the D10 culvert fields with the
defaults filled, re-uploads the mapped layer when anything changed, creates the
default 'Culvert 01' row when the project has none (waiting for its layer
factory before the PATCH), and attaches. create_scenario takes `culvert`.
"""

import importlib
import json
import uuid

import httpx
import pytest
import respx

from tests.test_server import (
    BASE,
    CALLER_BASIC,
    EXEC_ID,
    EXEC_STATUS_URL,
    ORIGIN,
    _call_as_caller,
    _dataset,
    _exec,
    _row,
    _tool_error_text,
    _tool_json,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _server(env_vars):
    """The server module, reloaded after the env vars are set (as in test_server)."""
    import hydrata_mcp.server as srv
    importlib.reload(srv)
    return srv

MAPPED_EXEC_ID = "c0ffee00-1111-4222-8333-444455556666"
MAPPED_STATUS_URL = f"{ORIGIN}/api/v2/resource-service/execution-status/{MAPPED_EXEC_ID}"
UPLOAD_URL = f"{ORIGIN}/api/v2/uploads/upload/"
CULVERTS = f"{BASE}/projects/42/culverts/"
D10 = (
    "name", "shape", "width_m", "height_m", "diameter_m", "upstream_invert_m",
    "downstream_invert_m", "barrels", "wall_thickness_m", "entrance_type", "material",
    "manning_n", "blockage_pct", "length_override_m", "description",
)
LINE = {"type": "MultiLineString", "coordinates": [[[300000.0, 6200000.0], [300020.0, 6200000.0]]]}
CRS = {"type": "name", "properties": {"name": "EPSG:32756"}}


def _fc(*props):
    return {"type": "FeatureCollection", "crs": CRS, "features": [
        {"type": "Feature", "id": f"towradgi_culverts.{i + 1}", "geometry": LINE, "properties": p}
        for i, p in enumerate(props)
    ]}


# Agent-style attribute names (width/height/barrels, label, pipe, us/ds_invert).
RAW_BOX = {"label": "Road box", "shape": "Box", "width": 2.4, "height": 1.2, "barrels": 3,
           "us_invert": 10.5, "ds_invert": "10.2", "survey_note": "from DEM"}
RAW_PIPE = {"anuga_label": "Brooker St", "shape": "pipe", "diameter": 0.9, "num_barrels": 2.0,
            "barrel_manning": 0.015, "upstream_invert_m": 8.0, "downstream_invert_m": 7.9,
            "blockage": 25, "is_bridge": False}


def _d10(**overrides):
    props = {key: None for key in D10}
    props.update({"name": "A", "shape": "box", "width_m": 1.5, "height_m": 1.0,
                  "upstream_invert_m": 5.0, "downstream_invert_m": 4.8, "barrels": 1,
                  "wall_thickness_m": 0.2, "entrance_type": "square_edge_headwall",
                  "material": "concrete", "manning_n": 0.013, "blockage_pct": 0.0})
    props.update(overrides)
    return props


def _mock_attach(fc, rows=None, source_alternate="geonode:towradgi_culverts"):
    """The routes of one culvert attach; returns them by name."""
    routes = {
        "status": respx.get(EXEC_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}]))),
        "source": respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
            return_value=httpx.Response(200, json=_dataset(1502, source_alternate))),
        "mapped_dataset": respx.get(f"{ORIGIN}/api/v2/datasets/1600/").mock(
            return_value=httpx.Response(200, json=_dataset(1600, "geonode:cul_42_culverts"))),
        "upload": respx.post(UPLOAD_URL).mock(
            return_value=httpx.Response(201, json={"execution_id": MAPPED_EXEC_ID})),
        "mapped_status": respx.get(MAPPED_STATUS_URL).mock(
            return_value=httpx.Response(200, json=_exec("finished", [{"id": 1600}]))),
        "list": respx.get(CULVERTS).mock(return_value=httpx.Response(
            200, json=rows if rows is not None else [_row(9, "Culvert 01", gn_layer=1450)])),
        "patch": respx.patch(f"{CULVERTS}9/").mock(
            side_effect=lambda request: httpx.Response(200, json=_row(9, "Culvert 01", gn_layer=json.loads(
                request.content)["gn_layer"]))),
        "project": respx.get(f"{BASE}/projects/42/").mock(
            return_value=httpx.Response(200, json={"id": 42, "base_map": 1525})),
    }

    def ows(request):
        if request.url.params.get("request") == "GetFeature" and \
                request.url.params.get("outputFormat") == "application/json":
            return httpx.Response(200, json=fc)
        return httpx.Response(200, text='<wfs:FeatureCollection numberMatched="2"/>')
    routes["ows"] = respx.get(url__startswith=f"{ORIGIN}/gs/ows").mock(side_effect=ows)
    respx.get(url__startswith=f"{ORIGIN}/api/v2/datasets/").mock(
        return_value=httpx.Response(200, json={"datasets": []}))
    respx.get(url__startswith=f"{ORIGIN}/api/v2/maps/").mock(
        return_value=httpx.Response(200, json={"map": {"maplayers": [{"name": "geonode:cul_42_culverts"}]}}))
    return routes


def _uploaded_collection(route):
    """The GeoJSON file the tool multipart-POSTed (parsed)."""
    request = route.calls[0].request
    body = request.content
    start = body.index(b"{")
    end = body.rindex(b"}") + 1
    return json.loads(body[start:end]), body


async def _attach(srv, **extra):
    args = {"project_id": 42, "kind": "culvert", "execution_id": EXEC_ID, "poll_interval_seconds": 0}
    args.update(extra)
    return _tool_json(await _call_as_caller(srv, "attach_input_layer", args))


class TestCulvertKindMapping:
    async def test_culvert_kind_maps_agent_names_onto_d10_with_defaults(self, _server):
        props, renamed, filled = _server._map_culvert_properties(RAW_BOX)
        assert props["name"] == "Road box"
        assert props["shape"] == "box"
        assert props["width_m"] == 2.4 and props["height_m"] == 1.2 and props["barrels"] == 3
        assert props["upstream_invert_m"] == 10.5 and props["downstream_invert_m"] == 10.2
        assert props["wall_thickness_m"] == 0.2 and props["entrance_type"] == "square_edge_headwall"
        assert props["material"] == "concrete" and props["manning_n"] == 0.013
        assert props["blockage_pct"] == 0.0
        assert props["diameter_m"] is None and props["length_override_m"] is None
        assert props["survey_note"] == "from DEM"  # unrecognised attributes are kept
        for alias in ("label", "width", "height", "us_invert", "ds_invert"):
            assert alias not in props
        assert renamed == {"label": "name", "width": "width_m", "height": "height_m",
                           "us_invert": "upstream_invert_m", "ds_invert": "downstream_invert_m"}
        assert set(filled) == {"wall_thickness_m", "entrance_type", "material", "blockage_pct", "manning_n"}

    async def test_culvert_kind_maps_pipe_to_circular_and_keeps_given_values(self, _server):
        props, renamed, filled = _server._map_culvert_properties(RAW_PIPE)
        assert props["shape"] == "circular" and props["diameter_m"] == 0.9
        assert props["barrels"] == 2 and isinstance(props["barrels"], int)
        assert props["manning_n"] == 0.015  # given, not the material default
        assert props["blockage_pct"] == 25.0
        assert props["name"] == "Brooker St"
        assert props["is_bridge"] is False
        assert "manning_n" not in filled and "blockage_pct" not in filled

    async def test_culvert_kind_d10_name_wins_over_an_alias(self, _server):
        props, renamed, _ = _server._map_culvert_properties({"width_m": 3.0, "width": 9.0, "Shape": "BOX"})
        assert props["width_m"] == 3.0 and "width" not in props and "width" not in renamed
        assert props["shape"] == "box" and "Shape" not in props

    async def test_culvert_kind_complete_d10_layer_needs_no_rewrite(self, _server):
        _, report = _server._map_culvert_collection(_fc(_d10(), _d10(name="B")))
        assert report["rewrite"] is False and report["renamed"] == {} and report["filled"] == {}
        # A WFS read that omits null columns reads the same.
        sparse = {k: v for k, v in _d10().items() if v is not None}
        assert _server._map_culvert_collection(_fc(sparse))[1]["rewrite"] is False

    async def test_culvert_kind_readback_of_a_mapped_layer_is_a_fixpoint(self, _server):
        """Shaped like localhost's real WFS read of a re-uploaded culvert layer
        (12-10-26, dataset 1490): GeoServer adds `fid` as a property and prints
        whole doubles as ints. Re-reading it must not trigger another re-upload."""
        readback = _d10(fid=1, blockage_pct=0, upstream_invert_m=9, owner="council")
        _, report = _server._map_culvert_collection(_fc(readback))
        assert report["rewrite"] is False

    async def test_culvert_kind_problems_name_what_the_build_will_refuse(self, _server):
        _, report = _server._map_culvert_collection(_fc(
            {"label": "No inverts", "shape": "box", "width": 1.0, "height": 1.0},
            {"shape": "arch", "upstream_invert_m": 1, "downstream_invert_m": 1, "barrels": 30},
        ))
        first, second = report["problems"]  # one line per culvert
        assert first == ("Culvert 'No inverts': upstream_invert_m is required; "
                         "downstream_invert_m is required")
        assert second.startswith("Culvert #2: shape must be 'box' or 'circular'")
        assert "barrels must be a whole number from 1 to 20" in second


class TestCulvertKindAttach:
    @respx.mock
    async def test_culvert_kind_attach_returns_the_layer_id_not_the_refusal(self, _server):
        routes = _mock_attach(_fc(RAW_BOX, RAW_PIPE))
        data = await _attach(_server)
        assert data["outcome"] == "attached"
        assert data["kind"] == "culvert" and data["route"] == "culverts"
        assert data["row_id"] == 9 and data["row_title"] == "Culvert 01"
        assert data["gn_layer"] == 1600 and data["dataset_pk"] == 1600
        assert data["dataset_alternate"] == "geonode:cul_42_culverts"
        assert "retry_execution_id" not in data
        assert json.loads(routes["patch"].calls[0].request.content) == {"gn_layer": 1600}
        for name in ("status", "ows", "upload", "mapped_status", "list", "patch"):
            assert routes[name].calls[0].request.headers["authorization"] == CALLER_BASIC, name

    @respx.mock
    async def test_culvert_kind_reuploads_mapped_values_with_defaults_filled(self, _server):
        routes = _mock_attach(_fc(RAW_BOX, RAW_PIPE))
        data = await _attach(_server)
        uploaded, body = _uploaded_collection(routes["upload"])
        assert b'name="base_file"; filename="cul_42_culverts.geojson"' in body
        assert uploaded["crs"] == CRS
        box, pipe = (f["properties"] for f in uploaded["features"])
        assert {k: box[k] for k in D10} == {
            "name": "Road box", "shape": "box", "width_m": 2.4, "height_m": 1.2, "diameter_m": None,
            "upstream_invert_m": 10.5, "downstream_invert_m": 10.2, "barrels": 3,
            "wall_thickness_m": 0.2, "entrance_type": "square_edge_headwall", "material": "concrete",
            "manning_n": 0.013, "blockage_pct": 0.0, "length_override_m": None, "description": None,
        }
        assert pipe["shape"] == "circular" and pipe["barrels"] == 2 and pipe["manning_n"] == 0.015
        assert uploaded["features"][0]["geometry"] == LINE
        mapped = data["mapped"]
        assert mapped["reuploaded"] is True and mapped["source_dataset_pk"] == 1502
        assert mapped["feature_count"] == 2
        assert mapped["renamed"]["width"] == "width_m" and mapped["renamed"]["label"] == "name"
        assert mapped["filled"]["wall_thickness_m"] == 2
        assert data["culvert_problems"] == [] and data["summary"]["culvert_problems"] == 0

    @respx.mock
    async def test_culvert_kind_d10_upload_attaches_as_is_without_reupload(self, _server):
        routes = _mock_attach(_fc(_d10()))
        data = await _attach(_server)
        assert not routes["upload"].called
        assert data["outcome"] == "attached" and data["dataset_pk"] == 1502
        assert json.loads(routes["patch"].calls[0].request.content) == {"gn_layer": 1502}
        assert data["mapped"]["reuploaded"] is False

    @respx.mock
    async def test_culvert_kind_creates_culvert_01_and_waits_for_its_layer(self, _server):
        routes = _mock_attach(_fc(_d10()), rows=[])
        create = respx.post(CULVERTS).mock(return_value=httpx.Response(201, json=_row(9, "Culvert 01")))
        detail = respx.get(f"{CULVERTS}9/").mock(side_effect=[
            httpx.Response(200, json=_row(9, "Culvert 01")),
            httpx.Response(200, json=_row(9, "Culvert 01", gn_layer=1450)),
        ])
        data = await _attach(_server)
        assert json.loads(create.calls[0].request.content) == {"title": "Culvert 01"}
        assert detail.call_count == 2  # waited until the layer factory set gn_layer
        assert data["created_row"] is True and data["outcome"] == "attached"
        assert json.loads(routes["patch"].calls[0].request.content) == {"gn_layer": 1502}

    @respx.mock
    async def test_culvert_kind_layer_factory_timeout_hands_back_the_retry_execution_id(self, _server):
        routes = _mock_attach(_fc(RAW_BOX), rows=[])
        respx.post(CULVERTS).mock(return_value=httpx.Response(201, json=_row(9, "Culvert 01")))
        respx.get(f"{CULVERTS}9/").mock(return_value=httpx.Response(200, json=_row(9, "Culvert 01")))
        data = await _attach(_server, timeout_seconds=0)
        assert data["outcome"] == "timed_out"
        assert data["retry_execution_id"] == str(uuid.UUID(MAPPED_EXEC_ID))
        assert not routes["patch"].called

    @respx.mock
    async def test_culvert_kind_reports_problems_and_says_so(self, _server):
        _mock_attach(_fc({"label": "No inverts", "shape": "box", "width": 1.0, "height": 1.0}))
        data = await _attach(_server)
        assert data["outcome"] == "attached"
        assert any("upstream_invert_m is required" in p for p in data["culvert_problems"])
        assert "need details filled in" in data["say_to_user"]

    @respx.mock
    async def test_culvert_kind_unreadable_upload_attaches_nothing(self, _server):
        routes = _mock_attach(_fc())
        routes["ows"].side_effect = None
        routes["ows"].return_value = httpx.Response(200, text="<ows:ExceptionReport/>")
        text = _tool_error_text(await _call_as_caller(_server, "attach_input_layer", {
            "project_id": 42, "kind": "culvert", "execution_id": EXEC_ID, "poll_interval_seconds": 0}))
        assert "nothing was attached" in text
        assert not routes["patch"].called and not routes["upload"].called


class TestCulvertKindScenario:
    @respx.mock
    async def test_culvert_kind_create_scenario_sends_culvert_and_reads_it_back(self, _server):
        post = respx.post(f"{BASE}/projects/42/scenarios/").mock(
            return_value=httpx.Response(201, json={"id": 77}))
        respx.get(f"{BASE}/projects/42/scenarios/77/").mock(
            return_value=httpx.Response(200, json={"id": 77, "name": "s", "culvert": 9}))
        respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1}))
        data = json.loads(await _server.create_scenario(
            project_id=42, name="s", resolution=10, duration=60, culvert=9))
        assert json.loads(post.calls[0].request.content)["culvert"] == 9
        assert data["culvert"] == 9

    @respx.mock
    async def test_culvert_kind_create_scenario_omits_culvert_when_not_given(self, _server):
        post = respx.post(f"{BASE}/projects/42/scenarios/").mock(
            return_value=httpx.Response(201, json={"id": 77}))
        respx.get(f"{BASE}/projects/42/scenarios/77/").mock(
            return_value=httpx.Response(200, json={"id": 77, "name": "s"}))
        respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1}))
        await _server.create_scenario(project_id=42, name="s", resolution=10, duration=60)
        assert "culvert" not in json.loads(post.calls[0].request.content)
