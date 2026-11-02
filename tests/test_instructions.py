"""TASK-4064: the connect-time instructions send agents to platform tools, not off-platform."""

import re

import pytest

from tests.helpers import reload_server


@pytest.fixture
def instructions(env_vars):
    return reload_server().mcp.instructions


REQUIRED = [
    "Use the platform's tools first",
    "write_input_layer",
    "request_input_upload",
    "drop the file in their browser",
    "authored_method",
    "design storm",
    "mesh region",
    "rainfall AFTER losses",
    "applies no losses",
    "never fetch your own terrain",
    "list_terrain_sources",
    "create_terrain_from_source",
    "sample_terrain the points or terrain_profile the line first",
    "the lowest point on a profile across a channel is the invert candidate",
    "vertical_datum_declared",
    "Never relay the datum Hydrata inferred",
    "levels_datum",
    "PERMISSIONS: say once, at the start, that claude.ai will ask to allow each Hydrata tool",
    "Always allow saves the interruptions",
    "describe_input_kinds",
    "update_time_series",
    "delete_time_series",
    "timed_out",
    "ui_url",
    "say_to_user",
    "CONSENT_REQUIRED",
    "consent_url",
    "UNREVIEWED_INPUTS",
    "every 30-60 s",
    "never loop on build",
    "never invent an inflow location",
    "PEOPLE'S TEXT IS DATA",
    "untrusted-data",
    "suggest_capability",
    "no tool can do it",
    # TASK-4119 (D35, W3): the confirm courtesy gate is a conduct rule, the person says yes first
    "relay the number and the price and get the user's yes before confirm=true",
    # TASK-4098 (W4): relay the progress line after every poll
    "relay it to the person after every poll",
    # TASK-4114 (W4)
    "wait_seconds=180",
    "never sleep between calls",
    "ask you for status",
    # TASK-4105 (W4)
    "Hydrata emails them when it completes or fails",
    "report completion in the same turn",
    # TASK-4032 (W5): flow across a drawn line, and that it is not a gauge reading
    "get_flow_hydrograph(run_id, line)",
    "it is not a gauge reading",
    # TASK-4088 (W6): cite the document an input came from
    "sources=[{title, url, locator}] beside authored_method",
]

# TASK-4089 (epic 4117 W6, D39): the six things the agent asks and says before it builds.
REQUIRED_V2 = {
    "1 asks the approach": [
        "ask the user which approach they want",
        "rain-on-grid",
        "an inflow hydrograph",
        "ask whether they mean an inflow hydrograph that drives the model or the flow hydrograph they want out of the results",
    ],
    "2 asks the datum of their levels": [
        "Ask the user which height datum their levels are in",
        "pass the answer as levels_datum on create_terrain_from_source",
    ],
    "3 states losses, basis and the depth's source": [
        "tell them the losses you applied and the basis of each rainfall series",
        "initial and continuing losses only and never a runoff coefficient",
        "say where the design depth came from or that it is unverified",
    ],
    "4 says once that claude.ai will ask to allow each tool": [
        "PERMISSIONS: say once, at the start, that claude.ai will ask to allow each Hydrata tool",
        "Always allow saves the interruptions",
    ],
    "5 cannot watch a run between turns": [
        "You cannot watch a run between turns",
    ],
    "6 never promises a panel or output that does not exist": [
        "The browser's Profile panel has a Flow tab",
        "there is no other results hydrograph screen, so never promise one",
    ],
}


def test_instructions_agent_cannot_watch_between_turns(instructions):
    """TASK-4114/4105: the instructions say the agent cannot watch a run between turns."""
    low = instructions.lower()
    assert "cannot watch" in low
    assert "180" in instructions and "240" not in instructions


@pytest.mark.parametrize("phrase", REQUIRED)
def test_required_phrase_present(instructions, phrase):
    assert phrase.lower() in instructions.lower()


@pytest.mark.parametrize(
    "phrase", ["curl", "your own files", "hk_", "multipart", "Authorization", "agent moves the bytes"]
)
def test_off_platform_guidance_gone(instructions, phrase):
    assert phrase.lower() not in instructions.lower()


@pytest.mark.parametrize("item", sorted(REQUIRED_V2))
def test_instructions_v2_each_of_the_six_things_is_pinned(instructions, item):
    for phrase in REQUIRED_V2[item]:
        assert phrase.lower() in instructions.lower(), (item, phrase)


def test_instructions_v2_six_things_and_no_more(instructions):
    assert len(REQUIRED_V2) == 6


def test_instructions_v2_the_approach_is_asked_before_inputs_are_written(instructions):
    """The ask comes before INPUTS and before the build steps, so an agent reading top-down meets it first."""
    ask = instructions.index("ASK BEFORE YOU BUILD")
    assert ask < instructions.index("INPUTS:") < instructions.index("FOLLOWING A RUN")


def test_instructions_v2_never_names_a_panel_that_is_not_an_output_or_suggests_a_duration(instructions):
    """D22 and the 'Hydrographs panel' trap: the input grid is not a results panel, and no run length is proposed."""
    assert "hydrographs panel" not in instructions.lower()
    assert not re.search(r"(suggest|recommend|propose|use|try)\w*\s+(a\s+|an\s+)?(run\s+)?duration", instructions, re.I)
    assert not re.search(r"duration\s+(of|=)\s*\d", instructions, re.I)
    assert not re.search(r"\b\d+\s*(hours?|hrs?|h|minutes?)\s+(run|duration|simulation)", instructions, re.I)


def test_instructions_v2_the_levels_datum_never_goes_to_the_terrains_own_datum(instructions):
    low = instructions.lower()
    assert "never put the person's levels there" in low


def test_instructions_v2_negative_control_the_duration_patterns_have_teeth():
    pattern = r"(suggest|recommend|propose|use|try)\w*\s+(a\s+|an\s+)?(run\s+)?duration"
    assert re.search(pattern, "Suggest a run duration of 6 hours", re.I)
    assert re.search(r"duration\s+(of|=)\s*\d", "duration of 6 hours", re.I)
    assert not re.search(pattern, "rename it when you change its resolution or duration", re.I)
