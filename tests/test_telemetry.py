"""TASK-3961 (epic 3958) — one mcp_tool_call record per tools/call.

Drives the REAL ASGI app (like tests/test_app.py) and reads the JSON lines the server prints to stdout.
Test names carry ``tool_call_record`` so the card's stored proof (``-k tool_call_record``) selects them.
"""

import json
from urllib.parse import quote

import httpx
import pytest
import respx
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.tools import ToolResult

from hydrata_mcp import telemetry
from tests.helpers import BASE, CALLER_KEY, KEY_HEADERS, MCP_HEADERS, _asgi_client, _parse, _tools_call

pytestmark = pytest.mark.asyncio

ACTOR = "user_id=12&username=ann&grant=34&credential=agent_key&internal=0"
UNICODE_NAME = "Zoë 李 & co"
EPIC_FIELDS = (
    "tool",
    "outcome",
    "duration_ms",
    "client_name",
    "oauth_client_id",
    "user_agent",
    "mcp.protocol.version",
    "request_id",
    "user_id",
    "username",
    "grant",
    "credential",
    "internal",
    "identity_source",
)


def _records(capsys, kind="call"):
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [r for r in lines if r.get("event") == "mcp_tool_call" and r.get("kind") == kind]


def _stdout_text(capsys):
    return capsys.readouterr().out


async def _call(srv, name="list_projects", arguments=None, headers=None):
    async with _asgi_client(srv) as c:
        resp = await c.post("/", headers={**KEY_HEADERS, **(headers or {})}, json=_tools_call(name, arguments))
    assert resp.status_code == 200, resp.text
    return _parse(resp)["result"]


def _register_probe(srv, calls_per_tool=0):
    """A throwaway tool that makes N Django calls (0, 1 or 2) and returns a constant."""
    mcp = srv.mcp

    @mcp.tool(name=f"probe_{calls_per_tool}")
    async def probe() -> str:
        for _ in range(calls_per_tool):
            await srv.client.get("/projects/")
        return json.dumps({"ok": True})


@pytest.mark.parametrize("django_calls", [0, 1, 2])
@respx.mock
async def test_tool_call_record_exactly_one_line_per_call(_server, capsys, django_calls):
    _register_probe(_server, django_calls)
    route = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={"results": []}))
    result = await _call(_server, f"probe_{django_calls}")
    assert result.get("isError") is not True
    assert route.call_count == django_calls
    records = _records(capsys)
    assert len(records) == 1
    assert records[0]["tool"] == f"probe_{django_calls}" and records[0]["outcome"] == "ok"


@respx.mock
async def test_tool_call_record_outcome_is_four_way_not_the_exception_class(_server, capsys):
    cases = [
        (httpx.Response(404, json={"detail": "nope"}), "refused", 404),
        (httpx.Response(403, json={"error_code": "AGENT_LEVEL_INSUFFICIENT"}), "refused", 403),
        (httpx.Response(429, json={"error_code": "RATE"}), "refused", 429),
        (httpx.Response(503, json={"error_code": "AGENT_PAUSED", "detail": "paused"}), "refused", 503),
        (httpx.Response(503, text="<html>bad gateway</html>"), "broken", 503),
        (httpx.Response(500, text="<html>Traceback</html>"), "broken", 500),
    ]
    for response, outcome, status in cases:
        respx.get(f"{BASE}/projects/").mock(return_value=response)
        result = await _call(_server, "list_projects")
        assert result["isError"] is True
        (record,) = _records(capsys)
        assert (record["outcome"], record["http_status"]) == (outcome, status), response
    paused = [c for c in cases if c[2] == 503][0]
    assert paused[1] == "refused"  # the kill switch never reads as broken


@respx.mock
async def test_tool_call_record_kill_switch_keeps_its_error_code(_server, capsys):
    respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(503, json={"error_code": "AGENT_PAUSED"}))
    await _call(_server, "list_projects")
    (record,) = _records(capsys)
    assert record["outcome"] == "refused" and record["error_code"] == "AGENT_PAUSED"


@respx.mock
async def test_tool_call_record_connect_error_and_timeout_are_broken(_server, capsys):
    for exc in (httpx.ConnectError("boom"), httpx.ReadTimeout("slow")):
        respx.get(f"{BASE}/projects/").mock(side_effect=exc)
        await _call(_server, "list_projects")
        (record,) = _records(capsys)
        assert record["outcome"] == "broken", exc


@respx.mock
async def test_tool_call_record_returned_422_is_refused(_server, capsys):
    respx.post(f"{BASE}/scenarios/9/run/").mock(
        return_value=httpx.Response(422, json={"error_code": "UNSIGNED_INPUTS", "detail": "sign them"})
    )
    result = await _call(_server, "start_simulation", {"scenario_id": 9})
    assert result["isError"] is True
    (record,) = _records(capsys)
    assert (record["outcome"], record["http_status"], record["error_code"]) == ("refused", 422, "UNSIGNED_INPUTS")


async def test_tool_call_record_returned_result_with_http_status_is_refused():
    for status, code in ((409, "CONSENT_REQUIRED"), (409, "UNREVIEWED_INPUTS"), (422, "MESH_TOO_LARGE")):
        result = ToolResult(content="{}", structured_content={"http_status": status, "error_code": code})
        assert telemetry.classify_result(result)[:3] == ("refused", status, code)
    ok = ToolResult(content="{}", structured_content={"http_status": 202})
    assert telemetry.classify_result(ok)[0] == "ok"
    five = ToolResult(content="{}", structured_content={"http_status": 502})
    assert telemetry.classify_result(five)[0] == "broken"


async def test_tool_call_record_invalid_for_unknown_tool_and_bad_arguments(_server, capsys):
    await _call(_server, "no_such_tool")
    await _call(_server, "get_project", {"project_id": "abc"})
    await _call(_server, "get_project", {})
    outcomes = [r["outcome"] for r in _records(capsys)]
    assert outcomes == ["invalid", "invalid", "invalid"]


async def test_tool_call_record_tool_error_and_status_less_api_error_are_invalid(_server):
    from hydrata_mcp.client import HydrataAPIError

    assert telemetry.classify_exception(ToolError("bad kind"))[0] == "invalid"
    assert telemetry.classify_exception(HydrataAPIError("bbox needs four numbers"))[0] == "invalid"
    assert telemetry.classify_exception(NotFoundError("Unknown tool"))[0] == "invalid"
    # an unexpected exception in our own code (FastMCP wraps it as ToolError from e) is our fault
    wrapped = ToolError("Error calling tool")
    wrapped.__cause__ = KeyError("x")
    assert telemetry.classify_exception(wrapped)[0] == "broken"


async def test_tool_call_record_walks_cause_and_context_chain(_server):
    from hydrata_mcp.client import HydrataAPIError

    inner = HydrataAPIError("refused", status=409, code="X")
    by_cause = ToolError("wrapped")
    by_cause.__cause__ = inner
    by_context = ToolError("wrapped")
    by_context.__context__ = inner
    assert telemetry.classify_exception(by_cause)[:3] == ("refused", 409, "X")
    assert telemetry.classify_exception(by_context)[:3] == ("refused", 409, "X")
    transport = HydrataAPIError("unreachable")
    transport.__context__ = httpx.ConnectError("x")  # client.py raises inside except, no `from`
    assert telemetry.classify_exception(transport)[0] == "broken"
    wrapped = ToolError("Error calling tool")
    wrapped.__cause__ = transport
    assert telemetry.classify_exception(wrapped)[0] == "broken"


@respx.mock
async def test_tool_call_record_identity_copied_from_actor_header(_server, capsys):
    header = f"user_id=12&username={quote(UNICODE_NAME)}&grant=34&credential=workos&internal=1"
    respx.get(f"{BASE}/projects/").mock(
        return_value=httpx.Response(200, json={"results": []}, headers={"X-Hydrata-Actor": header})
    )
    await _call(
        _server, "list_projects", headers={"user-agent": "claude-code/2.1.9", "mcp-protocol-version": "2025-06-18"}
    )
    (record,) = _records(capsys)
    assert record["user_id"] == 12 and record["username"] == UNICODE_NAME
    assert record["grant"] == 34 and record["credential"] == "workos"
    assert record["internal"] is True
    assert record["identity_source"] == "django"
    assert record["client_name"] == "claude-code" and record["client_version"] == "2.1.9"
    assert record["mcp.protocol.version"] == "2025-06-18"


@respx.mock
async def test_tool_call_record_internal_is_a_json_boolean_false_for_zero(_server, capsys):
    respx.get(f"{BASE}/projects/").mock(
        return_value=httpx.Response(200, json={"results": []}, headers={"X-Hydrata-Actor": ACTOR})
    )
    await _call(_server, "list_projects")
    (record,) = _records(capsys)
    assert record["internal"] is False and record["user_id"] == 12 and record["username"] == "ann"


@respx.mock
async def test_tool_call_record_actor_header_on_a_refusal_still_identifies(_server, capsys):
    respx.get(f"{BASE}/projects/").mock(
        return_value=httpx.Response(403, json={"error_code": "AGENT_KEY_SCOPE"}, headers={"X-Hydrata-Actor": ACTOR})
    )
    await _call(_server, "list_projects")
    (record,) = _records(capsys)
    assert record["outcome"] == "refused" and record["user_id"] == 12 and record["identity_source"] == "django"


@respx.mock
async def test_tool_call_record_first_reply_wins(_server, capsys):
    _register_probe(_server, 2)
    replies = iter(
        [
            httpx.Response(200, json={}, headers={"X-Hydrata-Actor": ACTOR}),
            httpx.Response(200, json={}, headers={"X-Hydrata-Actor": ACTOR.replace("user_id=12", "user_id=99")}),
        ]
    )
    respx.get(f"{BASE}/projects/").mock(side_effect=lambda request: next(replies))
    await _call(_server, "probe_2")
    (record,) = _records(capsys)
    assert record["user_id"] == 12


@respx.mock
async def test_tool_call_record_without_a_reply_falls_back_to_the_credential_fingerprint(_server, capsys):
    respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(401, json={"error_code": "AGENT_KEY_INVALID"}))
    await _call(_server, "list_projects")
    (record,) = _records(capsys)
    assert record["identity_source"] == "credential"
    assert record["user_id"] is None and record["username"] is None and record["internal"] is None
    assert record["identity"] == "hk_0123456789ab" and record["credential"] == "agent_key"
    assert CALLER_KEY.split()[1].split("_", 2)[2] not in json.dumps(record)  # the secret part never appears


async def test_tool_call_record_credential_fingerprint_forms():
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyXzEiLCJjbGllbnRfaWQiOiJjbGllbnRfQUJDIn0.sig"
    ident = telemetry.credential_identity(f"Bearer {jwt}")
    assert ident["fingerprint"].startswith("jwt_") and "user_1" not in ident["fingerprint"]
    assert ident["credential"] == "workos" and ident["oauth_client_id"] == "client_ABC"
    assert telemetry.credential_identity(None)["fingerprint"] is None
    assert telemetry.credential_identity("Bearer hk_short")["fingerprint"].startswith("hk_")


async def test_tool_call_record_malformed_actor_header_is_ignored():
    for bad in ("", "garbage", "user_id=abc&username=x", "username=x"):
        assert telemetry.parse_actor(bad) is None


@respx.mock
async def test_tool_call_record_forwards_the_inbound_request_id_on_every_call(_server, capsys):
    _register_probe(_server, 2)
    route = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={}))
    await _call(_server, "probe_2", headers={"X-Request-ID": "req-abc-123"})
    assert [c.request.headers["x-request-id"] for c in route.calls] == ["req-abc-123", "req-abc-123"]
    (record,) = _records(capsys)
    assert record["request_id"] == "req-abc-123"


@respx.mock
async def test_tool_call_record_mints_a_request_id_when_none_is_sent(_server, capsys):
    _register_probe(_server, 2)
    route = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={}))
    await _call(_server, "probe_2")
    sent = {c.request.headers["x-request-id"] for c in route.calls}
    (record,) = _records(capsys)
    assert sent == {record["request_id"]} and len(record["request_id"]) == 32


async def test_tool_call_record_request_id_is_cleaned():
    assert telemetry.clean_request_id("a\nb\x00c" + "x" * 100) == "abc" + "x" * 61


@respx.mock
async def test_tool_call_record_carries_every_epic_field(_server, capsys):
    respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={}, headers={"X-Hydrata-Actor": ACTOR}))
    await _call(_server, "list_projects", headers={"user-agent": "ua/1"})
    (record,) = _records(capsys)
    for name in EPIC_FIELDS:
        assert name in record, name
    assert record["event"] == "mcp_tool_call" and record["kind"] == "call"
    assert record["gen_ai.tool.name"] == "list_projects" and record["mcp.method.name"] == "tools/call"
    assert record["response_bytes"] > 0 and isinstance(record["duration_ms"], int)


@respx.mock
async def test_tool_call_record_ids_are_recorded_but_not_other_argument_values(_server, capsys):
    respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42}))
    await _call(_server, "get_project", {"project_id": 42})
    (record,) = _records(capsys)
    assert record["project_id"] == 42 and record["scenario_id"] is None


@respx.mock
async def test_tool_call_record_never_contains_arguments_tokens_or_presigned_urls(_server, capsys):
    canary = "CANARY-ARG-VALUE-7f3a"
    presigned = "https://bucket.s3.amazonaws.com/k?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIAxyz"
    respx.get(f"{BASE}/projects/").mock(
        return_value=httpx.Response(
            400, json={"detail": f"bad {presigned} for hk_0123456789ab_SECRETSECRETSECRET Bearer abc.def.ghi"}
        )
    )
    await _call(_server, "list_projects")
    await _call(_server, "get_project", {"project_id": canary})  # pydantic echoes input_value
    await _call(_server, canary)  # unknown tool name is a name, not a value, and is clipped
    out = _stdout_text(capsys)
    assert out.count('"kind":"call"') == 3
    for secret in ("X-Amz-Signature", "deadbeef", "SECRETSECRET", "abc.def.ghi"):
        assert secret not in out, secret
    records = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert canary not in [r for r in records if r["kind"] == "call"][1]["error"]


async def test_tool_call_record_error_text_is_clipped_to_300():
    assert len(telemetry.scrub("x" * 1000)) == 300
    assert "\n" not in telemetry.scrub("a\nb")


@respx.mock
async def test_tool_call_record_unauthenticated_requests_are_not_recorded_here(_server, capsys):
    async with _asgi_client(_server) as c:
        resp = await c.post("/", headers=MCP_HEADERS, json=_tools_call())
    assert resp.status_code == 401
    assert _records(capsys) == []


@respx.mock
async def test_tool_call_record_telemetry_failure_never_fails_the_call(_server, capsys, monkeypatch):
    respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={"results": []}))

    def boom(record):
        raise RuntimeError("stdout closed")

    monkeypatch.setattr(telemetry, "emit", boom)
    result = await _call(_server, "list_projects")
    assert result.get("isError") is not True


@respx.mock
async def test_tool_call_record_wrapped_409_keeps_its_status(_server, capsys):
    respx.delete(f"{BASE}/projects/42/time-series/7/").mock(
        return_value=httpx.Response(409, json={"error_code": "SERIES_IN_USE"})
    )
    result = await _call(_server, "delete_time_series", {"project_id": 42, "series_id": 7})
    assert result["isError"] is True
    (record,) = _records(capsys)
    assert (record["outcome"], record["http_status"], record["error_code"]) == ("refused", 409, "SERIES_IN_USE")


# TASK-4081: tool-side errors that wrap an upstream status carry it, so the outcome follows the status.
_LAYER_FC = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[151.0, -33.8], [151.01, -33.8]]},
            "properties": {"name": "C1"},
        }
    ],
}


@pytest.mark.parametrize(("status", "outcome"), [(500, "broken"), (503, "broken"), (422, "refused")])
@respx.mock
async def test_tool_call_record_write_input_layer_follows_the_upstream_status(_server, capsys, status, outcome):
    respx.post(f"{BASE}/projects/42/input-layers/write/").mock(
        return_value=httpx.Response(status, json={"detail": "nope", "error_code": "SOME_CODE"})
    )
    args = {"project_id": 42, "kind": "culvert", "features": _LAYER_FC, "method": "traced"}
    result = await _call(_server, "write_input_layer", args)
    assert result["isError"] is True
    (record,) = _records(capsys)
    assert (record["outcome"], record["http_status"]) == (outcome, status)


@pytest.mark.parametrize(("status", "outcome"), [(500, "broken"), (422, "refused")])
@respx.mock
async def test_tool_call_record_comparison_route_follows_the_upstream_status(_server, capsys, status, outcome):
    respx.get(f"{BASE}/runs/1591/comparison/").mock(return_value=httpx.Response(status, json={"detail": "x"}))
    result = await _call(_server, "get_observation_comparison", {"run_id": 1591})
    assert result["isError"] is True
    (record,) = _records(capsys)
    assert (record["outcome"], record["http_status"]) == (outcome, status)


@respx.mock
async def test_tool_call_record_describe_input_kinds_bad_body_is_broken(_server, capsys):
    respx.get(f"{BASE}/input-kinds/").mock(return_value=httpx.Response(200, json={"nope": 1}))
    result = await _call(_server, "describe_input_kinds")
    assert result["isError"] is True
    (record,) = _records(capsys)
    assert record["outcome"] == "broken"


async def test_tool_call_record_plain_validation_tool_error_is_still_invalid(_server, capsys):
    args = {"project_id": 42, "kind": "not_a_kind", "features": _LAYER_FC, "method": "traced"}
    result = await _call(_server, "write_input_layer", args)
    assert result["isError"] is True
    (record,) = _records(capsys)
    assert (record["outcome"], record["http_status"]) == ("invalid", None)
