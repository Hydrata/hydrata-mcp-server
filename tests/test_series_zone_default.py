"""TASK-3897 (epic 4492 W1) — a series created without a zone takes the PROJECT's zone, never Sydney.

Epic 3801 gave projects a time zone derived from where they are, and TASK-3933 made Hydrata's
time-series create read a series that arrives WITHOUT `timezone` in the project's zone
(hydrata apps/hydrology/serializers_v2.py, TimeSeriesSerializer.create). This server used to
send `timezone: "UTC"` whenever the agent gave none, so that project default never ran, and
its examples named Australia/Sydney. Now an omitted zone is omitted from the request, Hydrata
picks the project's zone, and the result reports the zone Hydrata saved.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from tests.helpers import BASE, _call_as_caller, _series_args, _series_row, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

SERIES_URL = f"{BASE}/projects/42/time-series/"
PROJECT_ZONE = "Africa/Dar_es_Salaam"  # what Hydrata derives for a project in Dar es Salaam
PACKAGE = Path(__file__).resolve().parent.parent / "hydrata_mcp"


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _args_without_zone(**overrides):
    args = _series_args(**overrides)
    del args["timezone"]
    return args


class TestSeriesZoneDefault:
    @respx.mock
    async def test_a_series_posted_without_a_zone_takes_the_project_zone_not_sydney(self, _server):
        route = respx.post(SERIES_URL).mock(
            return_value=httpx.Response(201, json=_series_row(77, timezone=PROJECT_ZONE))
        )
        data = _tool_json(await _call_as_caller(_server, "create_time_series", _args_without_zone()))
        body = json.loads(route.calls[0].request.content)
        # No zone is sent, so Hydrata's own default (the project's zone) applies.
        assert "timezone" not in body, body
        assert data["timezone"] == PROJECT_ZONE
        assert data["timezone"] != "Australia/Sydney"

    @respx.mock
    async def test_without_a_zone_the_result_never_claims_utc_or_sydney_when_hydrata_says_nothing(self, _server):
        # An older Hydrata reply without `timezone`: report null, not a zone this server made up.
        row = _series_row(77)
        del row["timezone"]
        respx.post(SERIES_URL).mock(return_value=httpx.Response(201, json=row))
        data = _tool_json(await _call_as_caller(_server, "create_time_series", _args_without_zone()))
        assert data["timezone"] is None

    @respx.mock
    async def test_an_explicit_zone_is_sent_as_given(self, _server):
        route = respx.post(SERIES_URL).mock(
            return_value=httpx.Response(201, json=_series_row(77, timezone="America/Chicago"))
        )
        data = _tool_json(
            await _call_as_caller(_server, "create_time_series", _series_args(timezone="America/Chicago"))
        )
        assert json.loads(route.calls[0].request.content)["timezone"] == "America/Chicago"
        assert data["timezone"] == "America/Chicago"

    async def test_the_timezone_argument_defaults_to_nothing_and_names_no_sydney(self, _server):
        tools = {tool.name: tool for tool in await _server.mcp.list_tools()}
        dumped = tools["create_time_series"].to_mcp_tool().model_dump(mode="json", by_alias=True)
        schema = dumped["inputSchema"]["properties"]["timezone"]
        assert schema.get("default") in ("", None), schema
        assert "Sydney" not in json.dumps(schema)
        assert "project" in schema["description"]

    async def test_no_tool_source_names_australia_sydney(self, _server):
        hits = [
            f"{path.name}:{n}"
            for path in sorted(PACKAGE.glob("*.py"))
            for n, line in enumerate(path.read_text().splitlines(), 1)
            if "Australia/Sydney" in line
        ]
        assert hits == []
