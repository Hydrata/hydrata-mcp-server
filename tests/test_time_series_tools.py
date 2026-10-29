"""TASK-4056 (epic 4051 W1.5) — update_time_series / delete_time_series."""

import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    CALLER_KEY,
    ROW_DATA,
    _call_as_caller,
    _series_row,
    _tool_error_text,
    _tool_json,
    reload_server,
)

pytestmark = pytest.mark.asyncio

URL = f"{BASE}/projects/42/time-series/77/"


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _stub_hand_back():
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 3}))


@respx.mock
async def test_time_series_update_patches_only_the_fields_given_and_forwards_the_key(_server):
    _stub_hand_back()
    route = respx.patch(URL).mock(return_value=httpx.Response(200, json=_series_row(77, authored_method="fixed")))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "update_time_series",
            {"project_id": 42, "series_id": 77, "data": {"rowData": ROW_DATA}, "method": " fixed the units "},
        )
    )
    assert route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert json.loads(route.calls[0].request.content) == {
        "data": {"rowData": ROW_DATA},
        "authored_method": "fixed the units",
    }
    assert data["id"] == 77 and data["row_count"] == 3 and data["http_status"] == 200


@respx.mock
async def test_time_series_update_with_nothing_to_change_is_refused_before_a_request(_server):
    route = respx.patch(URL).mock(return_value=httpx.Response(200, json={}))
    text = _tool_error_text(
        await _call_as_caller(
            _server, "update_time_series", {"project_id": 42, "series_id": 77, "method": "only a method"}
        )
    )
    assert "Nothing to change" in text and route.call_count == 0


@respx.mock
async def test_time_series_update_checks_the_row_shape_before_sending(_server):
    route = respx.patch(URL).mock(return_value=httpx.Response(200, json={}))
    text = _tool_error_text(
        await _call_as_caller(_server, "update_time_series", {"project_id": 42, "series_id": 77, "data": {"rows": []}})
    )
    assert "rowData" in text and route.call_count == 0


@respx.mock
async def test_time_series_update_relays_the_apis_plain_refusal(_server):
    respx.patch(URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error_code": "NEGATIVE_VALUES",
                "detail": "The series has 1 negative value (lowest -2): no.",
                "hint": "x",
            },
        )
    )
    text = _tool_error_text(
        await _call_as_caller(
            _server, "update_time_series", {"project_id": 42, "series_id": 77, "data": {"rowData": ROW_DATA}}
        )
    )
    assert "NEGATIVE_VALUES" in text and "negative value" in text


@respx.mock
async def test_time_series_delete_removes_the_series(_server):
    _stub_hand_back()
    route = respx.delete(URL).mock(return_value=httpx.Response(204))
    data = _tool_json(await _call_as_caller(_server, "delete_time_series", {"project_id": 42, "series_id": 77}))
    assert route.call_count == 1 and route.calls[0].request.headers["authorization"] == CALLER_KEY
    assert data["deleted"] is True and data["http_status"] == 204


@respx.mock
async def test_time_series_delete_attached_409_names_the_feature_and_says_nothing_was_deleted(_server):
    respx.delete(URL).mock(
        return_value=httpx.Response(
            409,
            json={"detail": 'This time series is still used by feature rai_5.7 of the layer "Rainfall 01". Detach it.'},
        )
    )
    text = _tool_error_text(await _call_as_caller(_server, "delete_time_series", {"project_id": 42, "series_id": 77}))
    assert "rai_5.7" in text and "Rainfall 01" in text and "Nothing was deleted" in text
