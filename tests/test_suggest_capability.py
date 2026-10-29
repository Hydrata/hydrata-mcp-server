"""TASK-3964 (epic 3958 D7) — suggest_capability: tell the Hydrata team a tool is missing."""

import json

import httpx
import pytest
import respx

from hydrata_mcp import telemetry
from tests.helpers import BASE, KEY_HEADERS, ORIGIN, _asgi_client, _parse, _tool_json, _tools_call

pytestmark = pytest.mark.asyncio

URL = f"{ORIGIN}/api/v2/agents/suggestions/"
ACTOR = "user_id=12&username=ann&grant=34&credential=agent_key&internal=0"


async def _call(srv, arguments):
    async with _asgi_client(srv) as c:
        resp = await c.post("/", headers=KEY_HEADERS, json=_tools_call("suggest_capability", arguments))
    return _parse(resp)["result"]


def _rows(capsys):
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]


async def test_suggest_capability_is_tool_36_with_read_only_annotations(_server):
    tools = await _server.mcp.list_tools()
    assert len(tools) == 36
    (tool,) = [t for t in tools if t.name == "suggest_capability"]
    annotations = tool.to_mcp_tool().annotations
    assert annotations.readOnlyHint is True and annotations.destructiveHint is False
    assert "no existing tool" in tool.description.lower() or "no tool" in tool.description.lower()


@respx.mock
async def test_suggest_capability_posts_to_django_and_yields_call_record_and_sample_with_its_identity(_server, capsys):
    route = respx.post(URL).mock(
        return_value=httpx.Response(201, json={"id": 5, "created_at": "x"}, headers={"X-Hydrata-Actor": ACTOR})
    )
    result = await _call(_server, {"what": "export results to CSV", "goal": "share with council"})
    data = _tool_json(result)
    assert data["say_to_user"] == "I've passed this request to the Hydrata team." and data["id"] == 5
    assert json.loads(route.calls[0].request.content) == {"what": "export results to CSV", "goal": "share with council"}
    assert route.calls[0].request.headers["authorization"].startswith("Bearer hk_")
    assert route.calls[0].request.headers["x-request-id"]
    call, sample = _rows(capsys)
    assert (call["kind"], call["tool"], call["outcome"], call["user_id"]) == ("call", "suggest_capability", "ok", 12)
    assert (sample["kind"], sample["user_id"], sample["username"], sample["request_id"]) == (
        "sample",
        12,
        "ann",
        call["request_id"],
    )
    assert sample["arguments"]["what"] == "export results to CSV"


@respx.mock
async def test_suggest_capability_goal_is_optional(_server):
    route = respx.post(URL).mock(return_value=httpx.Response(201, json={"id": 6}))
    await _call(_server, {"what": "a thing"})
    assert json.loads(route.calls[0].request.content) == {"what": "a thing"}


@respx.mock
async def test_suggest_capability_401_is_refused_and_the_sample_is_not_attributed_to_anyone_else(_server, capsys):
    respx.post(URL).mock(return_value=httpx.Response(401, json={"error_code": "AGENT_KEY_INVALID"}))
    result = await _call(_server, {"what": "a thing"})
    assert result["isError"] is True
    call, sample = _rows(capsys)
    assert call["outcome"] == "refused" and call["http_status"] == 401
    assert sample["user_id"] is None and sample["username"] is None
    assert sample["identity_source"] == "credential" and sample["identity"] == "hk_0123456789ab"


@respx.mock
async def test_suggest_capability_429_is_refused(_server, capsys):
    respx.post(URL).mock(
        return_value=httpx.Response(
            429, json={"error_code": "AGENT_SUGGESTION_LIMIT"}, headers={"X-Hydrata-Actor": ACTOR}
        )
    )
    await _call(_server, {"what": "a thing"})
    call = _rows(capsys)[0]
    assert (call["outcome"], call["error_code"]) == ("refused", "AGENT_SUGGESTION_LIMIT")


@respx.mock
async def test_suggest_capability_is_sampled_even_after_the_users_50_a_day_cap(_server, capsys):
    for i in range(telemetry.SAMPLE_DAILY_CAP):
        assert telemetry.SAMPLER.decide("user:12", f"t{i}", "refused")
    assert telemetry.SAMPLER.decide("user:12", "other", "refused") is False
    respx.post(URL).mock(return_value=httpx.Response(201, json={"id": 7}, headers={"X-Hydrata-Actor": ACTOR}))
    await _call(_server, {"what": "again"})
    await _call(_server, {"what": "and again"})
    assert [r["kind"] for r in _rows(capsys)] == ["call", "sample", "call", "sample"]
    # a stray route for the anuga base must not be hit: the endpoint lives at the origin
    assert BASE not in URL
