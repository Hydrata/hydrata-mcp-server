"""Tests for hydrata_mcp.client error wrapping."""

import httpx
import pytest
import respx

from hydrata_mcp.client import HydrataAPIError, HydrataClient


BASE = "https://hydrata.example.com/api/v2/anuga"


@pytest.fixture
def client(config):
    return HydrataClient(config)


class TestGetErrorWrapping:
    @respx.mock
    @pytest.mark.asyncio
    async def test_connect_error(self, client):
        respx.get(f"{BASE}/projects/").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(HydrataAPIError, match="unreachable"):
            await client.get("/projects/")

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout(self, client):
        respx.get(f"{BASE}/projects/").mock(side_effect=httpx.ReadTimeout("timed out"))
        with pytest.raises(HydrataAPIError, match="timed out"):
            await client.get("/projects/")

    @respx.mock
    @pytest.mark.asyncio
    async def test_404(self, client):
        respx.get(f"{BASE}/projects/999/").mock(return_value=httpx.Response(404))
        with pytest.raises(HydrataAPIError, match="404"):
            await client.get("/projects/999/")

    @respx.mock
    @pytest.mark.asyncio
    async def test_500(self, client):
        respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(500))
        with pytest.raises(HydrataAPIError, match="server error"):
            await client.get("/projects/")

    @respx.mock
    @pytest.mark.asyncio
    async def test_success(self, client):
        respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        data = await client.get("/projects/")
        assert data == {"results": []}


class TestPostErrorWrapping:
    @respx.mock
    @pytest.mark.asyncio
    async def test_connect_error(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(
            side_effect=httpx.ConnectError("refused")
        )
        with pytest.raises(HydrataAPIError, match="unreachable"):
            await client.post("/scenarios/1/run/", json={})

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        with pytest.raises(HydrataAPIError, match="timed out"):
            await client.post("/scenarios/1/run/", json={})

    @respx.mock
    @pytest.mark.asyncio
    async def test_409(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(return_value=httpx.Response(409))
        with pytest.raises(HydrataAPIError, match="409"):
            await client.post("/scenarios/1/run/", json={})

    @respx.mock
    @pytest.mark.asyncio
    async def test_500(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(return_value=httpx.Response(500))
        with pytest.raises(HydrataAPIError, match="server error"):
            await client.post("/scenarios/1/run/", json={})

    @respx.mock
    @pytest.mark.asyncio
    async def test_success_202(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(
            return_value=httpx.Response(202, json={"id": 42, "status": "queued"})
        )
        body, status = await client.post("/scenarios/1/run/", json={})
        assert status == 202
        assert body["id"] == 42

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_body(self, client):
        respx.post(f"{BASE}/runs/1/cancel/").mock(
            return_value=httpx.Response(200, content=b"")
        )
        body, status = await client.post("/runs/1/cancel/")
        assert body == {}
        assert status == 200
