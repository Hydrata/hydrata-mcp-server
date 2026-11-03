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
        respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={"results": []}))
        data = await client.get("/projects/")
        assert data == {"results": []}


class TestPostErrorWrapping:
    @respx.mock
    @pytest.mark.asyncio
    async def test_connect_error(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(HydrataAPIError, match="unreachable"):
            await client.post("/scenarios/1/run/", json={})

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(side_effect=httpx.ReadTimeout("timed out"))
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
        respx.post(f"{BASE}/runs/1/cancel/").mock(return_value=httpx.Response(200, content=b""))
        body, status = await client.post("/runs/1/cancel/")
        assert body == {}
        assert status == 200


class TestAgentPaused:
    """TASK-3510 (epic 3200): Hydrata's kill switch answers 503 AGENT_PAUSED with a
    human message. That is a deliberate refusal, not "the backend may be
    experiencing issues": the agent must see the message (do not retry, tell the
    person), so it is surfaced like a 4xx body."""

    PAUSED = {
        "error_code": "AGENT_PAUSED",
        "mode": "pause_launches",
        "detail": "Hydrata has paused run launches by agents, so nothing was started.",
    }

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_paused_post_surfaces_the_message(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(return_value=httpx.Response(503, json=self.PAUSED))
        with pytest.raises(HydrataAPIError) as exc:
            await client.post("/scenarios/1/run/", json={})
        text = str(exc.value)
        assert "AGENT_PAUSED" in text and "paused run launches" in text
        assert "experiencing issues" not in text

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_paused_post_is_refused_even_when_the_tool_reads_4xx_bodies(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(return_value=httpx.Response(503, json=self.PAUSED))
        with pytest.raises(HydrataAPIError, match="AGENT_PAUSED"):
            await client.post("/scenarios/1/run/", json={}, raise_for_status=False)

    @respx.mock
    @pytest.mark.asyncio
    async def test_any_other_503_is_still_the_generic_server_error(self, client):
        respx.post(f"{BASE}/scenarios/1/run/").mock(return_value=httpx.Response(503, text="<html>bad gateway</html>"))
        with pytest.raises(HydrataAPIError, match="server error"):
            await client.post("/scenarios/1/run/", json={})
