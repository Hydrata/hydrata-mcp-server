"""What the customer's agent reads in tools/list: every tool description and argument help.

Some clients (ChatGPT, openai-mcp/1.0.0) never pass the MCP server instructions to the model, so the
tool descriptions and argument help are all the agent sees. These tests read the LIVE tools/list.
"""

import json

import pytest

from tests.helpers import KEY_HEADERS, _asgi_client, _parse, _rpc

pytestmark = pytest.mark.asyncio


async def _tools(srv):
    async with _asgi_client(srv) as c:
        resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
    return {t["name"]: t for t in _parse(resp)["result"]["tools"]}


def _argument_help(tool, argument):
    return tool["inputSchema"]["properties"][argument].get("description", "")


# TASK-4289: no internal task id in anything the customer's agent reads (epic 4117 AC7).
async def test_no_tool_description_or_argument_help_carries_an_internal_task_id(_server):
    tools = await _tools(_server)
    leaks = []
    for name, tool in sorted(tools.items()):
        shown = {
            "description": tool.get("description", ""),
            "title": tool.get("title", ""),
            "inputSchema": json.dumps(tool.get("inputSchema", {}), ensure_ascii=False),
            "annotations": json.dumps(tool.get("annotations", {}), ensure_ascii=False),
        }
        leaks += [(name, field) for field, text in shown.items() if "TASK-" in text]
    assert not leaks, leaks


# TASK-4289: an agent should never learn a sources limit by failing; the help names the validator's limits.
@pytest.mark.parametrize(
    "tool_name", ["create_time_series", "update_time_series", "write_input_layer", "create_scenario"]
)
async def test_sources_help_states_the_limits_the_validator_enforces(_server, tool_name):
    from hydrata_mcp._helpers import SOURCES_MAX

    help_text = _argument_help((await _tools(_server))[tool_name], "sources")
    assert f"At most {SOURCES_MAX} entries" in help_text
    assert "title (1-200 characters)" in help_text
    assert "locator" in help_text and "at most 100 characters" in help_text
    assert "url (http or https, at most 2000 characters)" in help_text


async def test_sources_help_limits_match_the_validator_at_its_edges(_server):
    """The stated limits are the enforced ones: 200/100/20 pass, 201/101/21 are refused."""
    from fastmcp.exceptions import ToolError

    from hydrata_mcp._helpers import SOURCES_MAX, _clean_sources

    ok = {"title": "t" * 200, "url": "https://example.com/" + "u" * 1970, "locator": "l" * 100}
    assert _clean_sources([ok] * SOURCES_MAX)
    for bad in (
        [ok] * (SOURCES_MAX + 1),
        [{**ok, "title": "t" * 201}],
        [{**ok, "locator": "l" * 101}],
        [{**ok, "url": "https://example.com/" + "u" * 1990}],
    ):
        with pytest.raises(ToolError):
            _clean_sources(bad)


# TASK-4294: the engine (run_anuga run_utils._merge_timeseries) holds each rainfall / flow value until the next
# row (a step, no interpolation), so the help says so and agents stop inventing 1-second step rows.
@pytest.mark.parametrize("tool_name", ["create_time_series", "update_time_series"])
async def test_time_series_data_help_states_the_engine_step_rule(_server, tool_name):
    help_text = " ".join(_argument_help((await _tools(_server))[tool_name], "data").split())
    assert "each value holds from its timestamp until the next row's" in help_text
    assert "no straight line between rows" in help_text
    assert "ONE row per interval, at its START" in help_text
    assert "end with a 0 row at the end of the last interval" in help_text
    assert "never add a row just before the next one" in help_text
    assert "A stage series is the exception" in help_text


# TASK-4289 AC3: the two breakline refusal texts carry no internal task id.
async def test_breakline_refusals_carry_no_internal_task_id(_server):
    from fastmcp.exceptions import ToolError

    from hydrata_mcp.inputs import UNSUPPORTED_INPUT_LAYER_REASON

    assert "TASK-" not in UNSUPPORTED_INPUT_LAYER_REASON
    with pytest.raises(ToolError) as refused:
        await _server.attach_input_layer(project_id=42, kind="not-a-kind", execution_id="x")
    assert "breakline is refused" in str(refused.value) and "TASK-" not in str(refused.value)
