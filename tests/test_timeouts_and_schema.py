"""TASK-4063 / TASK-4514 — no tool waits past 110 s; start_simulation has no compute_backend.

TASK-4514 (epic 4509 W1.4, Decision 873): hydrata.com sits behind a CloudFront edge whose origin
response timeout is 120 s (the account quota), so a tool call silent for longer is cut and the agent
gets nothing. Every wait ceiling is therefore 110 s (was 200 s under claude.ai's 240 s cut), a
`timed_out` result tells the agent to call again, and no tool text promises a longer wait.
compute_backend stays only as a run-record field in results.
"""

import inspect
import json
import re

import pytest

pytestmark = pytest.mark.asyncio


EDGE_CEILING_S = 110  # every wait ceiling
EDGE_TIMEOUT_S = 120  # the edge cuts a silent origin request here: naming it is not a promise


async def test_wait_constants_fit_under_the_edge_timeout(_server):
    assert _server.POLL_MAX_TIMEOUT_SECONDS == EDGE_CEILING_S
    assert _server.POLL_DEFAULT_TIMEOUT_SECONDS <= _server.POLL_MAX_TIMEOUT_SECONDS
    from hydrata_mcp import runs, telemetry

    assert runs.RUN_WAIT_MAX_SECONDS == EDGE_CEILING_S
    # A cancel at or after the edge's 120 s cut is ours (broken), not the client walking away.
    assert telemetry.CANCEL_BROKEN_AFTER_S == EDGE_TIMEOUT_S


async def test_poll_until_clamps_oversized_timeout(_server):
    """A caller asking for 9999 s is clamped to the ceiling (never waits longer)."""
    clock = {"t": 0.0}
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    async def fetch():
        return {"status": "running"}

    orig_mono, orig_sleep = _server.time.monotonic, _server.asyncio.sleep
    _server.time.monotonic = lambda: clock["t"]
    _server.asyncio.sleep = fake_sleep
    try:
        _v, _polls, elapsed, timed_out = await _server._poll_until(fetch, lambda v: v["status"] == "done", 9999, 5)
    finally:
        _server.time.monotonic, _server.asyncio.sleep = orig_mono, orig_sleep
    assert timed_out is True
    assert elapsed <= EDGE_CEILING_S


async def test_every_timeout_default_is_within_ceiling(_server):
    tools = await _server.mcp.list_tools()
    seen = 0
    for tool in tools:
        for name, prop in tool.parameters.get("properties", {}).items():
            if (name.endswith("timeout_seconds") or name == "wait_seconds") and "default" in prop:
                seen += 1
                assert prop["default"] <= EDGE_CEILING_S, (tool.name, name, prop["default"])
            if name.endswith("timeout_seconds") or name == "wait_seconds":
                assert prop.get("maximum", 0) <= EDGE_CEILING_S, (tool.name, name, prop)
    assert seen >= 5


# A number that reads as a wait, a ceiling or a cut-off: "ceiling 200", "default 180", "max 180",
# "clamped to 200", "wait_seconds=180", "after 180 s", "at 240 s", "300 s".
_WAIT_NUMBER = re.compile(
    r"(?:ceiling|default|max|clamped to|wait_seconds=|after|at)\s*(\d+)\b(?!\s*(?:characters|items|rows|per))"
    r"|(\d+)\s+(?:s\b|seconds\b)"  # "180 s", never "401s" (a status)
)


def _promised_waits(text):
    return sorted(
        {
            int(a or b)
            for a, b in _WAIT_NUMBER.findall(text)
            if EDGE_CEILING_S < int(a or b) < 1000 and int(a or b) != EDGE_TIMEOUT_S
        }
    )


async def test_no_tool_text_promises_more_than_110(_server):
    """No description, parameter text or say_to_user string offers a wait above 110 s."""
    tools = await _server.mcp.list_tools()
    for tool in tools:
        text = (tool.description or "") + json.dumps(tool.parameters)
        assert not _promised_waits(text), (tool.name, _promised_waits(text))
    source = _server.module_source()
    assert not _promised_waits(source), _promised_waits(source)
    assert "claude.ai cuts a tool call at 240" not in source


def test_promised_wait_scan_sees_the_old_wording():
    assert _promised_waits("(default 180, ceiling 200 — claude.ai cuts a tool call at 240 s)") == [180, 200, 240]
    assert _promised_waits("call with wait_seconds=180") == [180]
    assert _promised_waits("up to 500 characters; default 110, ceiling 110; still 401s") == []


async def test_timed_out_still_says_call_again(_server):
    source = _server.module_source()
    assert "call build_scenario again with the same arguments" in source
    tools = {t.name: t for t in await _server.mcp.list_tools()}
    assert "timed_out" in tools["get_terrain"].description
    assert "call" in tools["get_terrain"].description.lower()


async def test_start_simulation_has_no_compute_backend(_server):
    assert "compute_backend" not in inspect.signature(_server.start_simulation).parameters
    tools = {t.name: t for t in await _server.mcp.list_tools()}
    tool = tools["start_simulation"]
    assert "compute_backend" not in json.dumps(tool.parameters)
    assert "compute_backend" not in (tool.description or "")


async def test_no_tool_schema_mentions_compute_backend(_server):
    for tool in await _server.mcp.list_tools():
        assert "compute_backend" not in json.dumps(tool.parameters), tool.name
