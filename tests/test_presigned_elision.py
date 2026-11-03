"""Presigned-URL elision, unit and per tool."""

import json

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    DEM_NAME,
    ELIDED,
    GN_LAYER_DEPTH_MAX,
    PROCESS_ID,
    RUN_S3_URL_KEYS,
    STAGING_KEY,
    _presigned_run,
    _signed_url,
    _terrain,
)

pytestmark = pytest.mark.asyncio


# Unsigned, relative — the only genuine top-level survivor on a terrain record.
BBOX_STATS_URL = "/api/v2/anuga/projects/16132/terrain/110535/bbox-stats/"


class TestElidePresignedUrls:
    """One recursive helper, applied per tool — never inside `_post_result`."""

    async def test_elide_presigned_walks_dicts_lists_and_leaves_plain_values_alone(self, _server):
        body = {
            "count": 1,
            "results": [
                {
                    "id": 67181,
                    "s3_package_url": _signed_url("runs/67181/package.zip"),
                    "nested": {"deep": [{"handoff": _signed_url("x")}]},
                    "gn_layer_depth_max": dict(GN_LAYER_DEPTH_MAX),
                    "bbox_stats_url": BBOX_STATS_URL,
                    "mesh_triangle_count": 256_869,
                    "error_message": None,
                    "flag": True,
                }
            ],
        }
        out = _server._elide_presigned(body)
        row = out["results"][0]
        assert out["count"] == 1
        assert row["s3_package_url"] == ELIDED
        assert row["nested"]["deep"][0]["handoff"] == ELIDED
        # Untouched: not a capability.
        assert row["gn_layer_depth_max"] == GN_LAYER_DEPTH_MAX
        assert row["bbox_stats_url"] == BBOX_STATS_URL
        assert row["mesh_triangle_count"] == 256_869
        assert row["error_message"] is None
        assert row["flag"] is True
        # The input is not mutated — the helper returns a new structure.
        assert "X-Amz-Signature" in body["results"][0]["s3_package_url"]

    async def test_elide_presigned_redacts_a_signed_url_in_place_inside_a_log_line(self, _server):
        log = (
            "2026-09-21 09:00:01 INFO downloading package from "
            + _signed_url("runs/67181/package.zip")
            + " to /tmp/anuga\n2026-09-21 09:04:12 INFO mesh has 256869 triangles"
        )
        out = _server._elide_presigned({"log": log})["log"]
        assert "X-Amz" not in out
        # Redacted IN PLACE: only the URL token went, the rest of the log survives.
        assert out.startswith("2026-09-21 09:00:01 INFO downloading package from " + ELIDED)
        assert "256869 triangles" in out
        assert "/tmp/anuga" in out

    async def test_elide_presigned_leaves_an_unsigned_s3_style_string_alone(self, _server):
        """Only a SIGNED string is a capability; an unsigned URL is just a link."""
        plain = "https://anuga-test-storage.s3.amazonaws.com/runs/67181/package.zip"
        assert _server._elide_presigned({"note": plain})["note"] == plain


class TestElidePresignedPerTool:
    """AC2: every read tool + start_simulation, asserted on the TOOL's output."""

    @respx.mock
    async def test_get_run_elides_the_five_presigned_urls_and_keeps_the_log(self, _server):
        run = _presigned_run(
            67181,
            "complete",
            log=(
                "2026-09-21 09:00:01 INFO uploading results to "
                + _signed_url("runs/67181/results.zip")
                + "\n2026-09-21 09:31:44 INFO run complete"
            ),
        )
        respx.get(f"{BASE}/runs/67181/").mock(return_value=httpx.Response(200, json=run))
        result = await _server.get_run(run_id=67181)
        assert "X-Amz" not in result
        data = json.loads(result)
        for key in RUN_S3_URL_KEYS:
            assert data[key] == ELIDED, key
        # AC6: the log is redacted in place, not dropped.
        assert "run complete" in data["log"] and ELIDED in data["log"]

    @respx.mock
    async def test_get_run_elide_keeps_the_gn_layer_dict_byte_equal(self, _server):
        """AC3 (over-elision guard): the nested `url`/`catalogURL` of a WMS dict
        are how results stay VIEWABLE after this card — assert by VALUE, because
        a "key is present" assertion on a dict cannot fail."""
        respx.get(f"{BASE}/runs/67181/").mock(return_value=httpx.Response(200, json=_presigned_run()))
        data = json.loads(await _server.get_run(run_id=67181))
        assert data["gn_layer_depth_max"] == GN_LAYER_DEPTH_MAX

    @respx.mock
    async def test_list_runs_elides_presigned_urls_nested_in_every_results_row(self, _server):
        """list_runs is the biggest leak by volume: five signed keys per row,
        default page_size 100 — and the body is a {count, results} DICT, so the
        helper has to RECURSE into the list to reach them."""
        respx.get(f"{BASE}/projects/42/runs/").mock(
            return_value=httpx.Response(
                200, json={"count": 2, "results": [_presigned_run(67180), _presigned_run(67181)]}
            )
        )
        result = await _server.list_runs(project_id=42)
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["count"] == 2
        assert [row["id"] for row in data["results"]] == [67180, 67181]
        assert all(row["s3_package_url"] == ELIDED for row in data["results"])

    @respx.mock
    async def test_start_simulation_elides_the_presigned_package_url_of_the_built_run(self, _server):
        """A `built` run ALREADY holds a signed package URL, and that is exactly
        the state start_simulation is called in — so every dispatch relayed a
        6-hour capability back into the transcript."""
        respx.post(f"{BASE}/scenarios/5/run/").mock(
            return_value=httpx.Response(202, json=_presigned_run(67182, "queued"))
        )
        result = await _server.start_simulation(scenario_id=5)
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["http_status"] == 202
        assert data["status"] == "queued"
        assert data["s3_package_url"] == ELIDED

    @respx.mock
    async def test_get_run_status_elides_a_presigned_url_under_an_unknown_key(self, _server):
        """get_run_status carries no signed key TODAY — the value-shape rule is
        what makes a key added server-side tomorrow safe by default."""
        respx.get(f"{BASE}/runs/67181/status/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 67181,
                    "status": "computing",
                    "progress_pct": 45,
                    "artifact_handoff": _signed_url("runs/67181/partial.zip"),
                },
            )
        )
        result = await _server.get_run_status(run_id=67181)
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["progress_pct"] == 45
        assert data["artifact_handoff"] == ELIDED

    @respx.mock
    async def test_get_project_elides_a_presigned_url_nested_under_an_unknown_key(self, _server):
        respx.get(f"{BASE}/projects/42/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 42,
                    "name": "Towradgi",
                    "base_map": 1525,
                    "exports": [{"handoff": _signed_url("exports/42.zip")}],
                },
            )
        )
        result = await _server.get_project(project_id=42)
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["name"] == "Towradgi" and data["base_map"] == 1525
        assert data["exports"][0]["handoff"] == ELIDED

    @respx.mock
    async def test_list_projects_elides_a_presigned_url_in_a_project_row(self, _server):
        respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "count": 1,
                    "results": [{"id": 42, "name": "Towradgi", "handoff": _signed_url("exports/42.zip")}],
                },
            )
        )
        result = await _server.list_projects()
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["results"][0]["name"] == "Towradgi"
        assert data["results"][0]["handoff"] == ELIDED

    @respx.mock
    async def test_get_terrain_elides_a_presigned_url_in_the_composed_result(self, _server):
        """get_terrain does NOT relay the body verbatim — it composes
        {outcome, status, polls, elapsed_seconds, terrain}: elide the COMPOSED
        result. AC3's other half rides here: `bbox_stats_url` is unsigned and
        relative and must survive EXACTLY."""
        terrain = _terrain(
            110535,
            "ready",
            bbox_stats_url=BBOX_STATS_URL,
            download_handoff=_signed_url("terrain/110535.tif"),
        )
        respx.get(f"{BASE}/projects/42/terrain/110535/").mock(return_value=httpx.Response(200, json=terrain))
        result = await _server.get_terrain(project_id=42, terrain_id=110535, timeout_seconds=0, poll_interval_seconds=0)
        assert "X-Amz" not in result
        data = json.loads(result)
        assert data["outcome"] == "ready" and data["status"] == "ready"
        assert data["terrain"]["bbox_stats_url"] == BBOX_STATS_URL
        assert data["terrain"]["download_handoff"] == ELIDED

    @respx.mock
    async def test_presign_terrain_upload_still_returns_its_presigned_url_intact(self, _server):
        """PRE-DECIDED #4 — the SOLE documented exception: the caller asked for
        this URL, so the helper must never be wired into `_post_result` (which
        presign/finalize/create_project/create_time_series/attach_input_layer/
        cancel_run/retry_run all share)."""
        upload_url = _signed_url("terrain_uploads/staging/dem.tif")
        respx.post(f"{BASE}/projects/42/terrain/upload/presign/").mock(
            return_value=httpx.Response(
                201,
                json={
                    "upload_url": upload_url,
                    "staging_key": STAGING_KEY,
                    "process_id": PROCESS_ID,
                    "method": "PUT",
                    "expires_in": 3600,
                },
            )
        )
        result = await _server.presign_terrain_upload(project_id=42, filename=DEM_NAME)
        assert json.loads(result)["upload_url"] == upload_url
        assert "X-Amz-Signature" in result
