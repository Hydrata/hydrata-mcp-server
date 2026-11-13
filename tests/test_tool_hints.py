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
    "get_terrain_merge",
    "sample_terrain",
    "terrain_profile",
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
    "convert_terrain_datum": False,  # makes a shifted copy; the source terrain is kept
    "finalize_terrain_upload": False,
    "presign_terrain_upload": False,
    "set_comparison_tolerance": False,
    "merge_terrains": False,  # a new merged terrain; the inputs are kept
    "set_levels_datum": False,  # the person's levels datum; the next call overwrites it
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


async def test_tool_hints_the_table_names_every_one_of_the_46_tools(_server):
    tools = await _tools(_server)
    assert len(tools) == 46
    grouped = READ_ONLY | set(WRITES) | SPENDS_COMPUTE
    assert grouped == set(tools), {"unlisted": sorted(set(tools) - grouped), "stale": sorted(grouped - set(tools))}
    assert len(READ_ONLY) + len(WRITES) + len(SPENDS_COMPUTE) == 46


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


# ---------------------------------------------------------------------------
# TASK-4288 (epic 4117 W8): the trust rules live in the tool descriptions too. ChatGPT (openai-mcp/1.0.0)
# never passes the server instructions to its model, so a rule only the instructions carry is a rule it
# never sees. Each tool that needs a rule carries it, in the LIVE tools/list.
# ---------------------------------------------------------------------------
ASK_APPROACH_PHRASES = (
    "ask the person which approach they want",
    "rain-on-grid",
    "an inflow hydrograph at a place they name",
    "by 'hydrograph' they mean an inflow that drives the model or the flow they want out of the results",
    "their design rainfall and losses",
    "do not choose or source these yourself",
)
NEVER_FETCH_TERRAIN = "Never fetch terrain from outside Hydrata"


def _flat(text):
    """A docstring wraps its lines; the agent reads the words."""
    return " ".join(text.split())


@pytest.mark.parametrize("name", ["write_input_layer", "create_time_series", "create_scenario"])
async def test_tool_rules_input_writers_say_ask_the_approach_and_the_design_rainfall(_server, name):
    description = _flat((await _tools(_server))[name]["description"])
    missing = [phrase for phrase in ASK_APPROACH_PHRASES if phrase not in description]
    assert not missing, (name, missing)


async def test_tool_rules_start_simulation_says_give_the_link_and_wait_for_the_go_ahead(_server):
    description = _flat((await _tools(_server))["start_simulation"]["description"])
    assert "give the person the ui_url and WAIT for their explicit go-ahead" in description
    assert "reviewed inputs are not a go-ahead" in description


@pytest.mark.parametrize("name", ["list_terrain_sources", "create_terrain_from_source"])
async def test_tool_rules_terrain_tools_say_never_fetch_your_own(_server, name):
    description = _flat((await _tools(_server))[name]["description"])
    assert NEVER_FETCH_TERRAIN in description and "where a Hydrata source covers the site" in description


async def test_tool_rules_server_instructions_are_unchanged(_server):
    """AC2: the rules were COPIED; the server instructions still carry them, word for word."""
    instructions = _server.mcp.instructions
    assert "TERRAIN: never fetch your own terrain where a platform source covers the area." in instructions
    assert "ASK BEFORE YOU BUILD: before you write any input or build, ask the user which approach" in instructions
    assert "WAIT for them to confirm they have reviewed the inputs in the map" in instructions
