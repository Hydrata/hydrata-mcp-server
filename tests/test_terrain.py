"""Terrain tools: create_project, presign, finalize and get_terrain."""

import json
import time

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    CALLER_KEY,
    DEM_NAME,
    KEY_HEADERS,
    PROCESS_ID,
    STAGING_KEY,
    _asgi_client,
    _call_as_caller,
    _parse,
    _rpc,
    _terrain,
    _tool_json,
)

pytestmark = pytest.mark.asyncio


DEM_SIZE = 31_833_087


class TestCreateProjectTool:
    @respx.mock
    async def test_create_project_posts_name_projection_and_forwards_the_key(self, _server):
        route = respx.post(f"{BASE}/projects/").mock(
            return_value=httpx.Response(201, json={"id": 42, "name": "mcp-w1-2469-t", "projection": "EPSG:32756"})
        )
        # TASK-3203 — the create body's base_map is null (the map is made async):
        # the tool re-reads the detail once.
        respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1525}))
        result = await _call_as_caller(_server, "create_project", {"name": "mcp-w1-2469-t", "projection": "EPSG:32756"})
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        # Exactly the two writable fields of ProjectCreateSerializerV2.
        assert json.loads(route.calls[0].request.content) == {
            "name": "mcp-w1-2469-t",
            "projection": "EPSG:32756",
        }
        data = _tool_json(result)
        assert data["id"] == 42
        assert data["http_status"] == 201
        assert data["base_map"] == 1525


class TestPresignTerrainUploadTool:
    @respx.mock
    async def test_presign_terrain_upload_posts_filename_type_size_and_forwards_the_key(self, _server):
        presigned = {
            "process_id": PROCESS_ID,
            "staging_key": STAGING_KEY,
            "upload_url": "https://anuga-test-storage.s3.amazonaws.com/x?X-Amz-Signature=abc",
            "method": "PUT",
            "expires_in": 3600,
            "content_type": "image/tiff",
        }
        route = respx.post(f"{BASE}/projects/42/terrain/upload/presign/").mock(
            return_value=httpx.Response(201, json=presigned)
        )
        result = await _call_as_caller(
            _server,
            "presign_terrain_upload",
            {"project_id": 42, "filename": DEM_NAME, "content_type": "image/tiff", "size": DEM_SIZE},
        )
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert json.loads(route.calls[0].request.content) == {
            "filename": DEM_NAME,
            "content_type": "image/tiff",
            "size": DEM_SIZE,
        }
        data = _tool_json(result)
        assert data["upload_url"] == presigned["upload_url"]
        assert data["staging_key"] == STAGING_KEY
        assert data["process_id"] == PROCESS_ID
        assert data["method"] == "PUT"
        assert data["http_status"] == 201

    @respx.mock
    async def test_presign_terrain_upload_defaults_image_tiff_and_omits_unknown_size(self, _server):
        """content_type defaults to image/tiff (the signed header the PUT must repeat);
        an unknown size is left OUT of the body — the API treats absent as unchecked."""
        route = respx.post(f"{BASE}/projects/42/terrain/upload/presign/").mock(
            return_value=httpx.Response(201, json={"upload_url": "u", "staging_key": STAGING_KEY})
        )
        await _call_as_caller(_server, "presign_terrain_upload", {"project_id": 42, "filename": DEM_NAME})
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert json.loads(route.calls[0].request.content) == {
            "filename": DEM_NAME,
            "content_type": "image/tiff",
        }


class TestFinalizeTerrainUploadTool:
    @respx.mock
    async def test_finalize_terrain_upload_posts_key_process_title_and_forwards_the_key(self, _server):
        route = respx.post(f"{BASE}/projects/42/terrain/upload/finalize/").mock(
            return_value=httpx.Response(202, json=_terrain(7, "creating"))
        )
        result = await _call_as_caller(
            _server,
            "finalize_terrain_upload",
            {
                "project_id": 42,
                "staging_key": STAGING_KEY,
                "process_id": PROCESS_ID,
                "title": "mcp-w1-2469-dem",
            },
        )
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert json.loads(route.calls[0].request.content) == {
            "staging_key": STAGING_KEY,
            "process_id": PROCESS_ID,
            "title": "mcp-w1-2469-dem",
        }
        data = _tool_json(result)
        assert data["id"] == 7
        assert data["status"] == "creating"
        assert data["http_status"] == 202

    @respx.mock
    async def test_finalize_terrain_upload_400_surfaces_the_api_detail(self, _server):
        """A finalize against a key whose PUT never landed is a 400 UPLOAD_NOT_FOUND.
        The client used to drop every 4xx body (reason phrase only), so the agent
        saw 'Bad Request' and could not tell a missing PUT from a bad key. The
        error text must now carry the API's error_code/detail."""
        from hydrata_mcp.client import HydrataAPIError

        respx.post(f"{BASE}/projects/42/terrain/upload/finalize/").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error_code": "UPLOAD_NOT_FOUND",
                    "detail": "No uploaded file found for that staging key.",
                },
            )
        )
        with pytest.raises(HydrataAPIError, match="400.*UPLOAD_NOT_FOUND") as exc_info:
            await _server.finalize_terrain_upload(project_id=42, staging_key=STAGING_KEY, process_id=PROCESS_ID)
        assert "No uploaded file found" in str(exc_info.value)


class TestGetTerrainTool:
    @respx.mock
    async def test_get_terrain_polls_until_ready_and_forwards_the_key(self, _server):
        route = respx.get(f"{BASE}/projects/42/terrain/7/").mock(
            side_effect=[
                httpx.Response(200, json=_terrain(7, "creating")),
                httpx.Response(200, json=_terrain(7, "styling")),
                httpx.Response(200, json=_terrain(7, "ready", gn_layer=1234)),
            ]
        )
        result = await _call_as_caller(
            _server,
            "get_terrain",
            {"project_id": 42, "terrain_id": 7, "poll_interval_seconds": 0},
        )
        assert route.call_count == 3
        for call in route.calls:
            assert call.request.headers["authorization"] == CALLER_KEY
        data = _tool_json(result)
        assert data["outcome"] == "ready"
        assert data["status"] == "ready"
        assert data["polls"] == 3
        assert data["terrain"]["gn_layer"] == 1234

    @respx.mock
    async def test_get_terrain_error_is_terminal_no_further_polls(self, _server):
        route = respx.get(f"{BASE}/projects/42/terrain/7/").mock(
            return_value=httpx.Response(200, json=_terrain(7, "error"))
        )
        data = _tool_json(
            await _call_as_caller(
                _server, "get_terrain", {"project_id": 42, "terrain_id": 7, "poll_interval_seconds": 0}
            )
        )
        assert route.call_count == 1
        assert data["outcome"] == "error"
        assert data["status"] == "error"

    @respx.mock
    async def test_get_terrain_timeout_is_bounded(self, _server):
        """timeout_seconds=0 → exactly ONE poll, then `timed_out` with the LAST status.

        The side effect answers `creating` five times and then a 500, so a
        regression that ignores the bound fails loudly (HydrataAPIError) instead
        of spinning forever under poll_interval_seconds=0.
        """
        calls = {"n": 0}

        def creating_then_500(request):
            calls["n"] += 1
            if calls["n"] > 5:
                return httpx.Response(500)
            return httpx.Response(200, json=_terrain(7, "creating"))

        route = respx.get(f"{BASE}/projects/42/terrain/7/").mock(side_effect=creating_then_500)
        data = _tool_json(
            await _call_as_caller(
                _server,
                "get_terrain",
                {
                    "project_id": 42,
                    "terrain_id": 7,
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0,
                },
            )
        )
        assert route.call_count == 1
        assert data["outcome"] == "timed_out"
        assert data["status"] == "creating"
        assert data["polls"] == 1

    @respx.mock
    async def test_get_terrain_sleep_is_capped_by_the_remaining_timeout(self, _server):
        """A poll interval LONGER than the timeout must not overshoot the bound.

        W1a sweep (epic 2467): the docstring promises "bounded by
        timeout_seconds", but an uncapped `asyncio.sleep(poll_interval_seconds)`
        made the wall clock ~interval when interval > timeout (interval 600 →
        one 600 s sleep → prod's 300 s /mcp/ proxy cuts the call and the agent
        gets nothing back). The sleep is now min(interval, remaining). This is
        a REAL 1 s wait on purpose: patching asyncio.sleep or time.monotonic
        would also patch the event loop's clock.
        """
        route = respx.get(f"{BASE}/projects/42/terrain/7/").mock(
            return_value=httpx.Response(200, json=_terrain(7, "creating"))
        )
        started = time.monotonic()
        data = _tool_json(
            await _call_as_caller(
                _server,
                "get_terrain",
                {
                    "project_id": 42,
                    "terrain_id": 7,
                    "timeout_seconds": 1,
                    "poll_interval_seconds": 60,
                },
            )
        )
        wall = time.monotonic() - started
        assert wall < 5, f"sleep was not capped by the timeout: {wall:.1f}s"
        assert data["outcome"] == "timed_out"
        assert data["status"] == "creating"
        # It DID sleep and re-poll (not an early return), just not for 60 s.
        assert route.call_count >= 2
        assert data["polls"] == route.call_count
        assert data["elapsed_seconds"] < 5

    @respx.mock
    async def test_get_terrain_without_id_reads_bare_list_and_picks_newest(self, _server):
        """GET /projects/<id>/terrain/ is a BARE JSON array (no count/results);
        with no terrain_id the tool follows the newest (highest id) row."""
        route = respx.get(f"{BASE}/projects/42/terrain/").mock(
            return_value=httpx.Response(200, json=[_terrain(3, "ready"), _terrain(9, "ready"), _terrain(5, "error")])
        )
        data = _tool_json(await _call_as_caller(_server, "get_terrain", {"project_id": 42, "poll_interval_seconds": 0}))
        assert route.calls[0].request.headers["authorization"] == CALLER_KEY
        assert data["terrain"]["id"] == 9
        assert data["outcome"] == "ready"
        assert data["terrain_count"] == 3

    @respx.mock
    async def test_get_terrain_empty_list_is_not_found_not_a_loop(self, _server):
        route = respx.get(f"{BASE}/projects/42/terrain/").mock(return_value=httpx.Response(200, json=[]))
        data = _tool_json(await _call_as_caller(_server, "get_terrain", {"project_id": 42, "poll_interval_seconds": 0}))
        assert route.call_count == 1
        assert data["outcome"] == "not_found"
        assert data["terrain"] is None

    async def test_terrain_tools_listed_with_cap_and_agent_moves_the_bytes(self, _server):
        """AC3: the descriptions carry the platform cap + 'the agent moves the bytes';
        no tool input is file contents (bytes/base64/path)."""
        async with _asgi_client(_server) as c:
            resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
        tools = {t["name"]: t for t in _parse(resp)["result"]["tools"]}
        for name in ("create_project", "presign_terrain_upload", "finalize_terrain_upload", "get_terrain"):
            assert name in tools, name
        presign = tools["presign_terrain_upload"]["description"]
        assert "5°" in presign and "40,000 km²" in presign
        assert "curl" in presign and "--upload-file" in presign
        # TASK-4064: a shell-less connector agent is sent to the platform source first.
        assert "create_terrain_from_source" in presign
        assert "moves the bytes" in presign
        for name in ("presign_terrain_upload", "finalize_terrain_upload"):
            props = tools[name]["inputSchema"]["properties"]
            for forbidden in ("file", "bytes", "base64", "content", "data", "path"):
                assert forbidden not in props, (name, forbidden)
