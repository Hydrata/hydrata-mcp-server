"""TASK-4087 (epic 4117 W6) - every tool's readOnlyHint / destructiveHint says what the tool does.

claude.ai asks the person to allow each tool the first time it is used; a client that groups or auto-allows by
annotation (the "Always allow" settings, a read-only group) trusts these hints. The table below is written by
hand, from what each tool does, in three groups, and is compared with the LIVE tools/list:

* READ_ONLY    - changes nothing the person owns (readOnlyHint true, destructiveHint false);
* WRITES       - changes a project, an input, a terrain or a series (readOnlyHint false); the ones that remove
                 something for good say destructiveHint true, every other write says false explicitly (the MCP
                 spec defaults it to TRUE);
* SPENDS_COMPUTE - starts a paid run, a build or a retry; never read-only, never destructive (a run can be
                 cancelled), and kept apart so a client can allow the reads and leave these on "ask".
"""

import pytest

from tests.helpers import KEY_HEADERS, _asgi_client, _parse, _rpc, reload_server

pytestmark = pytest.mark.asyncio

READ_ONLY = {
    "describe_input_kinds",
    "get_flow_hydrograph",  # a POST, but it only reads the playback store
    "get_observation_comparison",
    "get_project",
    "get_run",
    "get_run_status",
    "get_scenario",
    "get_terrain",
    "list_inputs",
    "list_observations",
    "list_projects",
    "list_runs",
    "list_terrain_sources",
    "list_time_series",
    "read_input_layer",
    "request_input_upload",  # mints a link for the person; changes no project data
    "suggest_capability",  # tells the Hydrata team about a gap; changes none of the user's data
}

# changes something, nothing here spends compute; value = destructive?
WRITES = {
    "add_observation_photo_link": False,
    "add_observations": False,
    "attach_input_layer": False,
    "clone_scenario": False,
    "create_observation_layer": False,
    "create_project": False,
    "create_scenario": False,
    "create_terrain_from_source": False,
    "create_time_series": False,
    "finalize_terrain_upload": False,
    "presign_terrain_upload": False,
    "set_comparison_tolerance": False,
    "set_observation_exclusion": False,
    "update_observation": False,
    "update_scenario": False,
    "update_time_series": False,
    "write_input_layer": False,
    # these remove something for good (a run's work, an observation and its photos, a series)
    "cancel_run": True,
    "delete_observation": True,
    "delete_time_series": True,
}

SPENDS_COMPUTE = {"build_scenario", "start_simulation", "retry_run"}


@pytest.fixture
def _server(env_vars):
    return reload_server()


async def _tools(srv):
    async with _asgi_client(srv) as c:
        resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
    return {t["name"]: t for t in _parse(resp)["result"]["tools"]}


def test_tool_hints_the_three_groups_are_disjoint():
    assert not READ_ONLY & set(WRITES)
    assert not READ_ONLY & SPENDS_COMPUTE
    assert not set(WRITES) & SPENDS_COMPUTE


async def test_tool_hints_the_table_names_every_one_of_the_40_tools(_server):
    tools = await _tools(_server)
    assert len(tools) == 40
    grouped = READ_ONLY | set(WRITES) | SPENDS_COMPUTE
    assert grouped == set(tools), {"unlisted": sorted(set(tools) - grouped), "stale": sorted(grouped - set(tools))}
    assert len(READ_ONLY) + len(WRITES) + len(SPENDS_COMPUTE) == 40


async def test_tool_hints_read_only_tools_say_read_only_and_not_destructive(_server):
    tools = await _tools(_server)
    for name in sorted(READ_ONLY):
        ann = tools[name]["annotations"]
        assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False, (name, ann)


async def test_tool_hints_writes_say_not_read_only_and_destructive_only_when_they_remove_something(_server):
    tools = await _tools(_server)
    for name, destructive in sorted(WRITES.items()):
        ann = tools[name]["annotations"]
        assert ann["readOnlyHint"] is False, (name, ann)
        assert ann["destructiveHint"] is destructive, (name, ann)


async def test_tool_hints_compute_spenders_are_writes_that_are_not_destructive(_server):
    tools = await _tools(_server)
    for name in sorted(SPENDS_COMPUTE):
        ann = tools[name]["annotations"]
        assert ann["readOnlyHint"] is False and ann["destructiveHint"] is False, (name, ann)


async def test_tool_hints_negative_control_a_mislabelled_tool_would_be_caught(_server):
    """Prove the comparison has teeth: a tool moved to the wrong group fails the same assertion."""
    tools = await _tools(_server)
    assert tools["start_simulation"]["annotations"]["readOnlyHint"] is False  # as a SPENDS_COMPUTE member
    assert "start_simulation" not in READ_ONLY  # were it listed there, the read-only test would fail on this hint
    assert tools["get_run"]["annotations"]["readOnlyHint"] is True and "get_run" in READ_ONLY
