"""TASK-4104 (epic 4117 W1, D31) — update_scenario and clone_scenario: thin wrappers, neither builds."""

import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    CALLER_KEY,
    SCENARIO_URL,
    _call_as_caller,
    _scenario,
    _tool_error_text,
    _tool_json,
    reload_server,
)

pytestmark = pytest.mark.asyncio

NEW_URL = f"{BASE}/projects/42/scenarios/6/"


@pytest.fixture
def _server(env_vars):
    return reload_server()


@respx.mock
async def test_update_scenario_patches_only_the_given_fields_and_never_builds(_server):
    patch = respx.patch(SCENARIO_URL).mock(return_value=httpx.Response(200, json={}))
    respx.get(SCENARIO_URL).mock(return_value=httpx.Response(200, json=_scenario(resolution=30.0, name="x - 30m")))
    build = respx.post(url__regex=r".*/build.*").mock(return_value=httpx.Response(200, json={}))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "update_scenario",
            {"project_id": 42, "scenario_id": 5, "name": "x - 30m", "resolution": 30},
        )
    )
    assert patch.calls[0].request.headers["authorization"] == CALLER_KEY
    assert json.loads(patch.calls[0].request.content) == {"name": "x - 30m", "resolution": 30}
    assert data["name"] == "x - 30m" and data["resolution"] == 30.0
    assert build.call_count == 0
    assert "Nothing was built" in data["say_to_user"]


@respx.mock
async def test_update_scenario_with_nothing_to_change_is_refused_before_any_request(_server):
    patch = respx.patch(SCENARIO_URL).mock(return_value=httpx.Response(200, json={}))
    text = _tool_error_text(await _call_as_caller(_server, "update_scenario", {"project_id": 42, "scenario_id": 5}))
    assert "nothing to change" in text and patch.call_count == 0


@respx.mock
async def test_clone_scenario_duplicates_then_names_and_overrides_the_copy_without_a_build(_server):
    dup = respx.post(f"{SCENARIO_URL}duplicate/").mock(return_value=httpx.Response(201, json={"id": 6}))
    patch = respx.patch(NEW_URL).mock(return_value=httpx.Response(200, json={}))
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, json=_scenario(id=6, name="x - rain 0.26")))
    build = respx.post(url__regex=r".*/build.*").mock(return_value=httpx.Response(200, json={}))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "clone_scenario",
            {"project_id": 42, "scenario_id": 5, "name": "x - rain 0.26", "overrides": {"rainfall": 99}},
        )
    )
    assert dup.call_count == 1
    assert json.loads(patch.calls[0].request.content) == {"rainfall": 99, "name": "x - rain 0.26"}
    assert data["id"] == 6 and build.call_count == 0
    assert "No run was copied" in data["say_to_user"]


@respx.mock
async def test_clone_scenario_without_name_or_overrides_makes_one_post_and_no_patch(_server):
    respx.post(f"{SCENARIO_URL}duplicate/").mock(return_value=httpx.Response(201, json={"id": 6}))
    patch = respx.patch(NEW_URL).mock(return_value=httpx.Response(200, json={}))
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, json=_scenario(id=6)))
    await _call_as_caller(_server, "clone_scenario", {"project_id": 42, "scenario_id": 5})
    assert patch.call_count == 0


@respx.mock
async def test_clone_scenario_refuses_an_unknown_override(_server):
    dup = respx.post(f"{SCENARIO_URL}duplicate/").mock(return_value=httpx.Response(201, json={"id": 6}))
    text = _tool_error_text(
        await _call_as_caller(
            _server, "clone_scenario", {"project_id": 42, "scenario_id": 5, "overrides": {"archived_at": "x"}}
        )
    )
    assert "archived_at" in text and dup.call_count == 0


async def test_instructions_cover_keeping_names_true_and_the_tools_are_listed(_server):
    text = _server.mcp.instructions.lower()
    assert "keep scenario names true" in text
    names = {t.name for t in await _server.mcp.list_tools()}
    assert {"update_scenario", "clone_scenario"} <= names
