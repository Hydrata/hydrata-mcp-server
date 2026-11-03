"""TASK-4052 (epic 4051 W1.1) — write_input_layer: the features travel in the call.

One POST to the sync endpoint, no importer, no execution_id; the key is forwarded;
a payload over ~150 KB is refused up front, naming the browser upload link; a
Hydrata refusal reaches the agent as a refusal.
"""

import json

import httpx
import pytest
import respx

from tests.helpers import BASE, CALLER_KEY, ORIGIN, _call_as_caller, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

WRITE = f"{BASE}/projects/42/input-layers/write/"
LINE = {"type": "LineString", "coordinates": [[151.0, -33.8], [151.01, -33.8]]}
FC = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": LINE, "properties": {"name": "C1"}}]}


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _written(kind="culvert"):
    return {
        "kind": kind,
        "row_id": 9,
        "row_title": "Culvert 01",
        "created_row": True,
        "gn_layer": 77,
        "dataset_alternate": "geonode:cul_42_culvert_01",
        "feature_count": 1,
    }


@respx.mock
async def test_write_input_layer_posts_the_features_once_and_forwards_the_key(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written()))
    respx.get(f"{ORIGIN}/api/v2/datasets/").mock(return_value=httpx.Response(200, json={"datasets": []}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 3}))
    data = _tool_json(
        await _call_as_caller(
            _server, "write_input_layer", {"project_id": 42, "kind": "culvert", "features": FC, "method": "traced"}
        )
    )
    assert route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert json.loads(route.calls[0].request.content) == {
        "kind": "culvert",
        "features": FC,
        "authored_method": "traced",
    }
    assert data["outcome"] == "written" and data["row_id"] == 9 and data["feature_count"] == 1


@respx.mock
async def test_write_input_layer_refuses_a_payload_over_the_cap_naming_the_upload_link(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written()))
    big = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": LINE, "properties": {"note": "x" * 200}} for _ in range(800)],
    }
    text = _tool_error_text(
        await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": "inflow", "features": big})
    )
    assert "KB" in text and "request_input_upload" in text and "Nothing was sent" in text
    assert not route.called


@respx.mock
async def test_write_input_layer_relays_a_hydrata_refusal(_server):
    respx.post(WRITE).mock(
        return_value=httpx.Response(
            400,
            json={
                "error_code": "VALIDATION_ERROR",
                "detail": "feature #2 is a Point; a inflow layer holds LineString features only",
            },
        )
    )
    text = _tool_error_text(
        await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": "inflow", "features": FC})
    )
    assert "feature #2 is a Point" in text


@respx.mock
async def test_write_input_layer_refuses_breakline_and_unknown_kinds_without_a_request(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written()))
    for kind in ("breakline", "terrain"):
        _tool_error_text(
            await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": kind, "features": FC})
        )
    assert not route.called


@respx.mock
async def test_write_input_layer_maps_the_legacy_structure_kind_to_building(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written("building")))
    respx.get(f"{ORIGIN}/api/v2/datasets/").mock(return_value=httpx.Response(200, json={"datasets": []}))
    data = _tool_json(
        await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": "structure", "features": FC})
    )
    assert json.loads(route.calls[0].request.content)["kind"] == "building"
    assert "deprecation" in data


@respx.mock
async def test_write_input_layer_append_mode_is_sent_and_the_say_names_what_was_kept(_server):
    body = {
        **_written(),
        "created_row": False,
        "mode": "append",
        "appended_fids": [5],
        "kept_features": 1,
        "feature_count": 2,
    }
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=body))
    respx.get(f"{ORIGIN}/api/v2/datasets/").mock(return_value=httpx.Response(200, json={"datasets": []}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 3}))
    data = _tool_json(
        await _call_as_caller(
            _server, "write_input_layer", {"project_id": 42, "kind": "culvert", "features": FC, "mode": "append"}
        )
    )
    assert json.loads(route.calls[0].request.content)["mode"] == "append"
    assert "beside the 1 already there" in data["say_to_user"]


@respx.mock
async def test_write_input_layer_replace_existing_is_forwarded_only_when_true_and_a_refusal_reaches_the_agent(_server):
    refusal = {"error_code": "REPLACE_REFUSED", "detail": "The culvert layer holds 1 feature(s) a person drew."}
    route = respx.post(WRITE).mock(return_value=httpx.Response(409, json=refusal))
    text = _tool_error_text(
        await _call_as_caller(
            _server, "write_input_layer", {"project_id": 42, "kind": "culvert", "features": FC, "mode": "replace"}
        )
    )
    assert "holds 1 feature" in text
    assert "replace_existing" not in json.loads(route.calls[0].request.content)
    await _call_as_caller(
        _server,
        "write_input_layer",
        {"project_id": 42, "kind": "culvert", "features": FC, "mode": "replace", "replace_existing": True},
    )
    assert json.loads(route.calls[1].request.content)["replace_existing"] is True


@respx.mock
async def test_write_input_layer_a_bad_mode_is_refused_before_any_request(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written()))
    text = _tool_error_text(
        await _call_as_caller(
            _server, "write_input_layer", {"project_id": 42, "kind": "culvert", "features": FC, "mode": "merge"}
        )
    )
    assert "append" in text and route.call_count == 0


@respx.mock
async def test_write_input_layer_target_row_and_replace_shared_are_forwarded_only_when_given(_server):
    route = respx.post(WRITE).mock(return_value=httpx.Response(201, json=_written()))
    respx.get(f"{ORIGIN}/api/v2/datasets/").mock(return_value=httpx.Response(200, json={"datasets": []}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 3}))
    await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": "culvert", "features": FC})
    assert set(json.loads(route.calls[0].request.content)) == {"kind", "features", "authored_method"}
    await _call_as_caller(
        _server,
        "write_input_layer",
        {"project_id": 42, "kind": "culvert", "features": FC, "target": "Rain variant", "replace_shared": True},
    )
    sent = json.loads(route.calls[1].request.content)
    assert sent["target"] == "Rain variant" and sent["replace_shared"] is True
    await _call_as_caller(
        _server, "write_input_layer", {"project_id": 42, "kind": "culvert", "features": FC, "target": 321}
    )
    assert json.loads(route.calls[2].request.content)["target"] == 321


@respx.mock
async def test_write_input_layer_target_row_refusal_naming_the_scenarios_reaches_the_agent(_server):
    refusal = {
        "error_code": "SHARED_ROW_REFUSED",
        "detail": "feeds scenario(s) with a built or complete run: #5 Sullivans",
    }
    respx.post(WRITE).mock(return_value=httpx.Response(409, json=refusal))
    text = _tool_error_text(
        await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": "culvert", "features": FC})
    )
    assert "#5 Sullivans" in text


@respx.mock
async def test_write_input_layer_target_row_default_write_says_which_scenarios_share_the_row(_server):
    body = {
        **_written(),
        "created_row": False,
        "mode": "replace",
        "targeted_row": False,
        "shared_by_scenarios": [{"id": 5, "name": "Sullivans", "has_built_or_complete_run": False}],
    }
    respx.post(WRITE).mock(return_value=httpx.Response(201, json=body))
    respx.get(f"{ORIGIN}/api/v2/datasets/").mock(return_value=httpx.Response(200, json={"datasets": []}))
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 3}))
    data = _tool_json(
        await _call_as_caller(_server, "write_input_layer", {"project_id": 42, "kind": "culvert", "features": FC})
    )
    assert "#5" in data["say_to_user"] and "shared" in data["say_to_user"]


async def test_instructions_cover_variants_a_copy_gets_its_own_input_row(_server):
    assert "a copy of a scenario gets its own input row" in _server.mcp.instructions.lower()
