"""TASK-4064: the connect-time instructions send agents to platform tools, not off-platform."""

import importlib

import pytest


@pytest.fixture
def instructions(env_vars):
    import hydrata_mcp.server as srv

    importlib.reload(srv)
    return srv.mcp.instructions


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
    "vertical_datum_declared",
    "Never relay the datum Hydrata inferred",
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
]


@pytest.mark.parametrize("phrase", REQUIRED)
def test_required_phrase_present(instructions, phrase):
    assert phrase.lower() in instructions.lower()


@pytest.mark.parametrize("phrase", ["curl", "your own files", "hk_", "multipart", "Authorization", "agent moves the bytes"])
def test_off_platform_guidance_gone(instructions, phrase):
    assert phrase.lower() not in instructions.lower()
