"""TASK-4689 (epic 4674 W3.1, D10): the agent tools offer no fast screen.

delineate_catchments has no mode argument (a new line runs High resolution; an existing line runs again with run_again), no
sentence or argument help says fast screen or talks of a copy of a line, the server instructions say neither, and
get_stream_corridor takes no grid (the API reads the High resolution analysis only).
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from tests.helpers import BASE, _call_as_caller, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "tools.json"
LIST = f"{BASE}/projects/42/delineations/"
ONE = f"{LIST}9/"
LINE = [[151.7280, -32.9100], [151.7300, -32.9120]]
FULL = {
    "mode": "full",
    "cells": 90_000,
    "working_resolution_m": 1.0,
    "runs_on": "web_server",
    "can_run": True,
    "expected_seconds": {"low": 10, "high": 60},
    "can_take_hours": False,
}


@pytest.fixture
def _server(env_vars):
    return reload_server()


async def _schema(srv, name):
    tools = {t.name: t for t in await srv.mcp.list_tools()}
    return tools[name].to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)


async def test_no_quick_mode_the_delineate_tool_has_no_mode_parameter(_server):
    schema = await _schema(_server, "delineate_catchments")
    props = schema["inputSchema"]["properties"]
    assert "mode" not in props
    assert "run_again" in props
    text = json.dumps(schema).lower()
    assert "fast screen" not in text and "quick" not in text and "(high res)" not in text and "copy" not in text


async def test_no_quick_mode_get_delineation_says_nothing_of_a_fast_screen_or_a_copy(_server):
    text = json.dumps(await _schema(_server, "get_delineation")).lower()
    assert "fast screen" not in text and "quick" not in text and "copy" not in text


async def test_no_quick_mode_the_instructions_never_say_fast_screen(_server):
    words = _server.mcp.instructions.lower()
    assert "fast screen" not in words
    assert "mode 'quick'" not in words and "(high res)" not in words
    assert "high resolution" in words


async def test_no_quick_mode_get_stream_corridor_accepts_no_quick_grid(_server):
    schema = await _schema(_server, "get_stream_corridor")
    assert "grid" not in schema["inputSchema"]["properties"]
    assert "quick" not in json.dumps(schema).lower()
    error = _tool_error_text(
        await _call_as_caller(
            _server,
            "get_stream_corridor",
            {"project_id": 42, "terrain_id": 37, "threshold_ha": 50, "half_width_m": 40, "grid": "quick"},
        )
    )
    assert "grid" in error.lower()


async def test_no_quick_mode_no_tool_in_the_snapshot_says_fast_screen():
    for tool in json.loads(SNAPSHOT.read_text()):
        assert "fast screen" not in json.dumps(tool).lower(), tool["name"]


@respx.mock
async def test_no_quick_mode_a_new_line_is_posted_with_no_mode(_server):
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    respx.get(f"{LIST}estimate/").mock(return_value=httpx.Response(200, json={"terrain_id": 37, "full": FULL}))
    post = respx.post(LIST).mock(
        return_value=httpx.Response(
            202,
            json={
                "delineation_id": 9,
                "line": {"id": 9, "name": "Rail"},
                "delineation": {},
                "job": {"status": "pending"},
            },
        )
    )
    data = _tool_json(
        await _call_as_caller(_server, "delineate_catchments", {"project_id": 42, "line": LINE, "name": "Rail"})
    )
    assert "mode" not in json.loads(post.calls[0].request.content)
    assert data["outcome"] == "started"
    assert "fast screen" not in data["say_to_user"].lower() and "copy" not in data["say_to_user"].lower()


@respx.mock
async def test_no_quick_mode_run_again_posts_the_run_with_no_mode(_server):
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    run = respx.post(f"{ONE}run/").mock(
        return_value=httpx.Response(
            202,
            json={
                "delineation_id": 9,
                "job": {"status": "pending", "mode": "full", "runs_on": "web_server"},
                "delineation": {},
            },
        )
    )
    respx.get(ONE).mock(return_value=httpx.Response(200, json={"line": {"id": 9, "name": "Rail"}, "job": {}}))
    respx.get(f"{ONE}estimate/").mock(return_value=httpx.Response(200, json={"terrain_id": 37, "full": FULL}))
    data = _tool_json(
        await _call_as_caller(
            _server, "delineate_catchments", {"project_id": 42, "delineation_id": 9, "run_again": True}
        )
    )
    assert json.loads(run.calls[0].request.content) == {}
    assert data["outcome"] == "started" and data["delineation_id"] == 9
    assert "created_copy" not in data


@respx.mock
async def test_no_quick_mode_an_old_coarse_result_is_relayed_in_the_apis_words(_server):
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": 77}))
    respx.get(ONE).mock(
        return_value=httpx.Response(
            200,
            json={
                "line": {"id": 9, "name": "Rail"},
                "delineation": {"created_by": "ann"},
                "tally": {"catchments": 2, "total_area_ha": 10.0},
                "shown_result": {
                    "mode": "quick",
                    "old_fast_screen": True,
                    "fast_screen": True,
                    "label": "Old result",
                    "fix_sentence": "Run it again.",
                    "profile_available": False,
                },
                "job": {"status": "complete", "mode": "quick"},
            },
        )
    )
    data = _tool_json(
        await _call_as_caller(_server, "get_delineation", {"project_id": 42, "delineation_id": 9, "timeout_seconds": 0})
    )
    assert data["resolution"] == "Old result" and data["profile_available"] is False
    assert "Old result at " in data["say_to_user"] and "Run it again." in data["say_to_user"]
