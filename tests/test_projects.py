"""Project read tools: list_projects and get_project."""

import json

import httpx
import pytest
import respx

from tests.helpers import BASE

pytestmark = pytest.mark.asyncio


class TestListProjects:
    @respx.mock
    async def test_returns_json(self, _server):
        respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={"count": 0, "results": []}))
        result = await _server.list_projects()
        data = json.loads(result)
        assert data["count"] == 0
        assert data["results"] == []

    @respx.mock
    async def test_pagination_params(self, _server):
        route = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={"count": 0, "results": []}))
        await _server.list_projects(page=2, page_size=10)
        assert route.calls[0].request.url.params["page"] == "2"
        assert route.calls[0].request.url.params["page_size"] == "10"


class TestGetProject:
    @respx.mock
    async def test_returns_project(self, _server):
        project = {"id": 1, "name": "Test Project", "projection": "EPSG:28356"}
        respx.get(f"{BASE}/projects/1/").mock(return_value=httpx.Response(200, json=project))
        result = await _server.get_project(project_id=1)
        assert json.loads(result)["name"] == "Test Project"

    @respx.mock
    async def test_not_found(self, _server):
        from hydrata_mcp.client import HydrataAPIError

        respx.get(f"{BASE}/projects/999/").mock(return_value=httpx.Response(404))
        with pytest.raises(HydrataAPIError, match="404"):
            await _server.get_project(project_id=999)


class TestProjectLevelsDatum:
    """TASK-4118 (D32): get_project relays the project's levels datum and any mismatch with its terrain."""

    @respx.mock
    async def test_levels_datum_is_relayed_with_a_mismatch_warning(self, _server):
        levels = {"datum": "NAVD88", "label": "NAVD88", "declared_by": "dave", "declared_at": None}
        respx.get(f"{BASE}/projects/1/").mock(
            return_value=httpx.Response(200, json={"id": 1, "name": "P", "base_map": 5, "levels_datum": levels})
        )
        respx.get(f"{BASE}/projects/1/terrain/").mock(
            return_value=httpx.Response(
                200, json=[{"id": 3, "levels_datum_mismatch": "Your levels are on NAVD88 but this terrain is on AHD."}]
            )
        )
        data = json.loads(await _server.get_project(project_id=1))
        assert data["levels_datum"] == levels
        assert "NAVD88" in data["say_to_user"] and "WARNING" in data["say_to_user"]

    @respx.mock
    async def test_levels_datum_unset_asks_the_user(self, _server):
        respx.get(f"{BASE}/projects/1/").mock(
            return_value=httpx.Response(200, json={"id": 1, "name": "P", "base_map": 5})
        )
        respx.get(f"{BASE}/projects/1/terrain/").mock(return_value=httpx.Response(200, json=[]))
        data = json.loads(await _server.get_project(project_id=1))
        assert data["levels_datum"] is None and "ask the user" in data["say_to_user"]

    @respx.mock
    async def test_levels_datum_unset_with_a_terrain_names_set_levels_datum(self, _server):
        """TASK-4376: the project has its terrain, so the answer is recorded with set_levels_datum (as get_terrain says)."""
        respx.get(f"{BASE}/projects/1/").mock(
            return_value=httpx.Response(200, json={"id": 1, "name": "P", "base_map": 5})
        )
        respx.get(f"{BASE}/projects/1/terrain/").mock(return_value=httpx.Response(200, json=[{"id": 3}]))
        say = json.loads(await _server.get_project(project_id=1))["say_to_user"]
        assert "ask the user" in say and "set_levels_datum" in say
        assert "create_terrain_from_source" not in say

    @respx.mock
    async def test_levels_datum_unset_without_a_terrain_does_not_name_set_levels_datum(self, _server):
        respx.get(f"{BASE}/projects/1/").mock(
            return_value=httpx.Response(200, json={"id": 1, "name": "P", "base_map": 5})
        )
        respx.get(f"{BASE}/projects/1/terrain/").mock(return_value=httpx.Response(200, json=[]))
        say = json.loads(await _server.get_project(project_id=1))["say_to_user"]
        assert "ask the user" in say and "set_levels_datum" not in say
