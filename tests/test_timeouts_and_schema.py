"""TASK-4063 (epic 4051 W4.2) — no tool waits past 200 s; start_simulation has no compute_backend.

claude.ai cuts a tool call at 240 s, so every poll ceiling is <= 200 s and a `timed_out` result
tells the agent to call again. compute_backend stays only as a run-record field in results.
"""

import inspect
import json

import pytest

pytestmark = pytest.mark.asyncio


async def test_poll_constants_are_at_most_200(_server):
    assert _server.POLL_MAX_TIMEOUT_SECONDS <= 200
    assert _server.POLL_DEFAULT_TIMEOUT_SECONDS <= _server.POLL_MAX_TIMEOUT_SECONDS


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
    assert elapsed <= 200


async def test_every_timeout_default_is_within_ceiling(_server):
    tools = await _server.mcp.list_tools()
    seen = 0
    for tool in tools:
        for name, prop in tool.parameters.get("properties", {}).items():
            if name.endswith("timeout_seconds") and "default" in prop:
                seen += 1
                assert prop["default"] <= 200, (tool.name, name, prop["default"])
    assert seen >= 4


async def test_no_tool_text_promises_more_than_200(_server):
    source = _server.module_source()
    assert "ceiling 280" not in source and "default 240" not in source


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
