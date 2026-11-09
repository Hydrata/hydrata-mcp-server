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
    _series_args,
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


class TestRainfallBasis:
    """TASK-4091 (epic 4117 W2; D38, D39): a rainfall series states its basis, excess or gross."""

    @respx.mock
    async def test_basis_is_required_for_a_hyetograph_and_nothing_is_sent_without_it(self, _server):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        args = _series_args()
        del args["rainfall_basis"]
        text = _tool_error_text(await _call_as_caller(_server, "create_time_series", args))
        assert "rainfall_basis" in text and "excess" in text and "gross" in text
        assert route.call_count == 0

    @respx.mock
    async def test_basis_must_be_excess_or_gross(self, _server):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        text = _tool_error_text(
            await _call_as_caller(_server, "create_time_series", _series_args(rainfall_basis="unspecified"))
        )
        assert "rainfall_basis" in text and route.call_count == 0

    @respx.mock
    @pytest.mark.parametrize("basis", ["excess", "gross"])
    async def test_basis_is_sent_and_echoed(self, _server, basis):
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77, rainfall_basis=basis))
        )
        data = _tool_json(await _call_as_caller(_server, "create_time_series", _series_args(rainfall_basis=basis)))
        assert json.loads(route.calls[0].request.content)["rainfall_basis"] == basis
        assert data["rainfall_basis"] == basis

    @respx.mock
    async def test_basis_is_not_required_or_sent_for_a_hydrograph(self, _server):
        """NEGATIVE CONTROL."""
        route = respx.post(f"{BASE}/projects/42/time-series/").mock(
            return_value=httpx.Response(201, json=_series_row(77))
        )
        args = _series_args(series_type="hydrograph", units="m3/s")
        del args["rainfall_basis"]
        await _call_as_caller(_server, "create_time_series", args)
        assert "rainfall_basis" not in json.loads(route.calls[0].request.content)

    @respx.mock
    async def test_update_time_series_accepts_the_basis(self, _server):
        route = respx.patch(f"{BASE}/projects/42/time-series/77/").mock(
            return_value=httpx.Response(200, json=_series_row(77, rainfall_basis="excess"))
        )
        _stub_hand_back()
        data = _tool_json(
            await _call_as_caller(
                _server, "update_time_series", {"project_id": 42, "series_id": 77, "rainfall_basis": "excess"}
            )
        )
        assert json.loads(route.calls[0].request.content) == {"rainfall_basis": "excess"}
        assert data["rainfall_basis"] == "excess"

    async def test_update_time_series_refuses_another_basis(self, _server):
        text = _tool_error_text(
            await _call_as_caller(
                _server, "update_time_series", {"project_id": 42, "series_id": 77, "rainfall_basis": "net"}
            )
        )
        assert "rainfall_basis" in text

    async def test_the_instructions_tell_the_agent_to_state_the_basis(self, _server):
        text = _server.mcp.instructions
        assert "rainfall_basis" in text and "`excess`" in text and "`gross`" in text and "twice" in text


async def _registered_tools(server):
    from tests.helpers import KEY_HEADERS, _asgi_client, _parse, _rpc

    async with _asgi_client(server) as c:
        resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
    return {t["name"]: t for t in _parse(resp)["result"]["tools"]}


class TestTimestampOffsetHelp:
    """TASK-4295 (epic 3754 W2, D4): the help says what an offset means, as Hydrata now stores it: a stamp with
    Z or +hh:mm is that instant (stored in UTC), a stamp without one is the series' wall clock, never mixed."""

    RULE = (
        "a stamp with no offset is a wall clock in the series timezone",
        "a stamp with Z or +hh:mm is that instant, stored in UTC and shown in the series timezone",
        "one series never mixes the two",
    )

    @pytest.mark.parametrize(
        "tool,param",
        [
            ("create_time_series", "data"),
            ("create_time_series", "timezone"),
            ("update_time_series", "data"),
        ],
    )
    async def test_the_registered_help_states_the_offset_rule(self, _server, tool, param):
        tools = await _registered_tools(_server)
        help_text = " ".join(tools[tool]["inputSchema"]["properties"][param]["description"].split())
        for phrase in self.RULE:
            assert phrase in help_text, (tool, param, phrase)

    async def test_no_help_still_says_a_trailing_z_is_tolerated(self, _server):
        tools = await _registered_tools(_server)
        flat = " ".join(json.dumps(tools[name]) for name in ("create_time_series", "update_time_series"))
        assert "tolerated" not in flat

    async def test_the_mixed_refusal_reaches_the_agent_in_plain_words(self, _server):
        with respx.mock:
            _stub_hand_back()
            respx.patch(URL).mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error_code": "MIXED_TIMESTAMP_FORMS",
                        "detail": "Row 1 ('2026-01-01T00:00:00') has no time offset, but row 2 has one.",
                        "hint": "Send every row with an offset, or every row without one.",
                    },
                )
            )
            rows = {
                "rowData": [
                    {"timestamp": "2026-01-01T00:00:00", "value": 1.0},
                    {"timestamp": "2025-12-31T14:00:00Z", "value": 0.0},
                ]
            }
            text = _tool_error_text(
                await _call_as_caller(_server, "update_time_series", {"project_id": 42, "series_id": 77, "data": rows})
            )
        assert "MIXED_TIMESTAMP_FORMS" in text or "no time offset" in text


class TestLastRowRule:
    """TASK-4296 (epic 3754 W2): Hydrata's summary now counts a series' last row as run_anuga applies it
    (run_utils._merge_timeseries: a rainfall's last row lasts one more interval then 0, an inflow's last value
    is held to the run end). The hold_last_value help states the same rule, and the agent's sentence says when
    a flow volume stops at the series' last row."""

    async def test_hold_last_value_help_states_the_engine_last_row_rule(self, _server):
        tools = await _registered_tools(_server)
        for name in ("create_time_series", "update_time_series"):
            help_text = " ".join(tools[name]["inputSchema"]["properties"]["hold_last_value"]["description"].split())
            assert "a rainfall's last row rains for one more interval (as long as the step before it)" in help_text
            assert "then stops at 0" in help_text
            assert "an inflow's last value is held to the end of the run" in help_text

    async def test_a_held_flow_volume_to_the_series_end_says_so(self, _server):
        from hydrata_mcp.inputs import _series_sentence

        result = {"series_type": "hydrograph", "name": "Held flow", "units": "m3/s", "hold_last_value": True}
        summary = {
            "peak_value": 2.0,
            "peak_at": "2026-01-01T00:05:00+00:00",
            "total": 300.0,
            "total_units": "m3",
            "total_to": "series_end",
            "trailing_value": 2.0,
        }
        sentence = _series_sentence(result, summary)
        assert "volume 300 m3 to the series' last row" in sentence
        assert "keeps the last flow going to the end of the run" in sentence

    @pytest.mark.parametrize(
        "summary",
        [
            {"total": 300.0, "total_units": "m3", "total_to": "series_end", "trailing_value": 0.0},
            {"total": 2100.0, "total_units": "m3", "total_to": "run_end", "trailing_value": 2.0},
            {"total": 3.0, "total_units": "mm", "total_to": "rain_end", "trailing_value": 24.0},
        ],
    )
    async def test_negative_control_no_note_when_nothing_is_held_past_the_total(self, _server, summary):
        from hydrata_mcp.inputs import _series_sentence

        sentence = _series_sentence({"series_type": "hydrograph", "name": "S"}, summary)
        assert "series' last row" not in sentence
