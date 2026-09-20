"""Hydrata MCP Server — 13 hand-crafted tools for ANUGA flood simulation."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from starlette.middleware import Middleware

from .client import HydrataClient
from .config import Config

config = Config.from_env()
client = HydrataClient(config)


# ---------------------------------------------------------------------------
# TASK-3166 (W0.1, epic 2467) — credential pass-through
# ---------------------------------------------------------------------------
class RequireAuthForToolsCall:
    """Pure-ASGI middleware: 401 an anonymous ``tools/call``; every other method stays open.

    The server holds no identity of its own. A ``tools/call`` must carry the caller's
    ``Authorization`` header (HTTP Basic for a hydrata.com account), which
    :class:`HydrataClient` forwards verbatim to the REST API — so GeoNode scopes
    results by the real user's project permissions and the audit trail names them.
    Missing it, the request is answered here with ``401`` +
    ``WWW-Authenticate: Basic realm="hydrata.com"`` and a JSON-RPC-shaped error body
    (code ``-32001``) that an MCP client can surface, before fastmcp ever runs a tool.

    Why only ``tools/call`` (decision D2 on TASK-2467, a deliberate deviation from
    TASK-1389's "reject anonymous tools/list"): the tool catalog is public on GitHub
    anyway, and ``initialize`` / ``tools/list`` / ``ping`` must keep answering
    anonymously so the MCP registry's liveness check stays true.

    Why this shape and not the built-ins: ``fastmcp.server.auth`` is Bearer-only and
    gates every method; Starlette's ``BaseHTTPMiddleware`` needs explicit body
    replay. This reads the body once, decides, and replays it to the app untouched.
    A body that is not JSON (or not a dict/list, or nested too deep to decode) is
    passed through as-is so fastmcp emits its own ``-32700`` parse error — never
    a 500 from here. Batches (arrays) are inspected element-wise as defence in
    depth; the MCP SDK itself rejects them downstream.

    Expected side effect: OAuth-aware MCP clients that see the 401 probe
    ``/.well-known/oauth-protected-resource`` and get a 404. That is fine — we
    are not an OAuth resource server; Basic is the credential.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        # Drain the request body (may arrive in chunks); stop on a disconnect.
        chunks: list[bytes] = []
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        request_id = None
        needs_auth = False
        try:
            parsed = json.loads(body)
        except (ValueError, RecursionError):
            # RecursionError: a pathologically nested body (~100 KB of '[')
            # overflows the decoder and is NOT a ValueError; it must be treated
            # like any other unparseable body, or it escapes as a 500 (W0 sweep).
            parsed = None
        for msg in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(msg, dict) and msg.get("method") == "tools/call":
                needs_auth = True
                request_id = msg.get("id")
                break

        # ASGI header names are lowercase bytes; a blank value is not a credential.
        has_auth = any(
            name == b"authorization" and value.strip()
            for name, value in scope.get("headers", [])
        )

        if needs_auth and not has_auth:
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32001,
                        "message": (
                            "Authentication required: send HTTP Basic credentials "
                            "for a hydrata.com account"
                        ),
                    },
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode()),
                        (b"www-authenticate", b'Basic realm="hydrata.com"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload, "more_body": False})
            return

        # Replay the drained body once, then hand the real receive back so a
        # later disconnect still reaches the app.
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                if disconnected:
                    return {"type": "http.disconnect"}
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


# One list, baked into the one `app` below, which BOTH entry points serve: prod's
# unit file (`uvicorn hydrata_mcp.server:app`) and the `hydrata-mcp` CLI. An
# unguarded CLI entry would be a second, anonymous server.
MIDDLEWARE = [Middleware(RequireAuthForToolsCall)]


@asynccontextmanager
async def lifespan(server):
    """Close the httpx connection pool on shutdown."""
    yield
    await client.close()


mcp = FastMCP(
    "Hydrata",
    instructions=(
        "Hydrata is a geospatial hydraulic modeling platform. Use these tools to "
        "manage ANUGA flood simulation projects, scenarios, and runs. "
        "A typical workflow is: list_projects → get_scenario → start_simulation → "
        "poll get_run_status until complete → get_run for results. "
        "To author a project from your own files: create_project → "
        "presign_terrain_upload → PUT the GeoTIFF yourself (curl --upload-file) → "
        "finalize_terrain_upload → get_terrain (polls until ready). No tool accepts "
        "file contents; the agent moves the bytes."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Tool 1: list_projects
# ---------------------------------------------------------------------------
@mcp.tool
async def list_projects(
    page: Annotated[int, "Page number (default 1)"] = 1,
    page_size: Annotated[int, "Results per page, max 100 (default 100)"] = 100,
) -> str:
    """List ANUGA simulation projects accessible to the authenticated user.

    Returns a paginated list of projects with their names, projections,
    and base map references.
    """
    data = await client.get("/projects/", params={"page": page, "page_size": page_size})
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 2: get_project
# ---------------------------------------------------------------------------
@mcp.tool
async def get_project(
    project_id: Annotated[int, "The project ID"],
) -> str:
    """Get details of a specific ANUGA project including its scenarios.

    Returns the project name, projection (EPSG code), base map ID,
    and configuration.
    """
    data = await client.get(f"/projects/{project_id}/")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 3: get_scenario
# ---------------------------------------------------------------------------
@mcp.tool
async def get_scenario(
    project_id: Annotated[int, "The project ID"],
    scenario_id: Annotated[int, "The scenario ID"],
) -> str:
    """Get scenario details including its current status and latest run.

    The status field is computed from the latest run and will be one of:
    created, building, built, queued, computing, processing, complete,
    error, or cancelled. A scenario must be in 'built' status before
    it can be run.
    """
    data = await client.get(f"/projects/{project_id}/scenarios/{scenario_id}/")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 4: start_simulation
# ---------------------------------------------------------------------------
@mcp.tool
async def start_simulation(
    scenario_id: Annotated[int, "The scenario ID to run"],
    compute_backend: Annotated[
        str,
        "Compute backend: 'local' (Celery), 'ec2' (dedicated instance), "
        "or 'batch' (AWS Batch spot, cheapest). Default: 'local'",
    ] = "local",
) -> str:
    """Start a flood simulation run for a built scenario.

    The scenario must be in 'built' status. Returns 202 with the new run.
    The run transitions through: built → queued → computing → processing → complete.

    After starting, poll get_run_status to track progress. Returns 409
    if the scenario is not in the correct state.
    """
    body, status_code = await client.post(
        f"/scenarios/{scenario_id}/run/",
        json={"compute_backend": compute_backend},
    )
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 5: get_run_status
# ---------------------------------------------------------------------------
@mcp.tool
async def get_run_status(
    run_id: Annotated[int, "The run ID"],
) -> str:
    """Lightweight status check for a simulation run (fast, <50ms).

    Use this for polling instead of get_run. Returns only: id, status,
    progress_pct (0-100), eta_seconds, error_message, and compute_backend.

    Poll every 5-10 seconds. Terminal states: complete, error, cancelled.
    """
    data = await client.get(f"/runs/{run_id}/status/")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 6: get_run
# ---------------------------------------------------------------------------
@mcp.tool
async def get_run(
    run_id: Annotated[int, "The run ID"],
) -> str:
    """Get full details of a simulation run including timing and results.

    Returns the complete run record: status, progress, timing (start/end
    timestamps, duration), compute details (backend, instance type, cost),
    mesh info, error messages, and result log. Use get_run_status for
    lightweight polling; use this for final results.
    """
    data = await client.get(f"/runs/{run_id}/")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 7: cancel_run
# ---------------------------------------------------------------------------
@mcp.tool
async def cancel_run(
    run_id: Annotated[int, "The run ID to cancel"],
) -> str:
    """Cancel an in-flight simulation run.

    Works on runs in built, queued, or computing status. Cleans up
    compute resources (terminates EC2 instance, Celery task, or Batch job).

    Returns 409 if the run is already in a terminal state
    (complete, cancelled, or error).
    """
    body, status_code = await client.post(f"/runs/{run_id}/cancel/")
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 8: retry_run
# ---------------------------------------------------------------------------
@mcp.tool
async def retry_run(
    run_id: Annotated[int, "The run ID to retry"],
) -> str:
    """Retry a failed simulation run.

    Resets an errored run back to 'created' status and triggers a new
    package build. The same run ID is reused. Only valid when
    status is 'error'. Returns 409 for any other state.
    """
    body, status_code = await client.post(f"/runs/{run_id}/retry/")
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 9: list_runs
# ---------------------------------------------------------------------------
@mcp.tool
async def list_runs(
    project_id: Annotated[int, "The project ID"],
    status_filter: Annotated[
        str | None,
        "Filter by status: created, building, built, queued, computing, "
        "processing, complete, error, cancelled. Omit for all.",
    ] = None,
    page: Annotated[int, "Page number (default 1)"] = 1,
    page_size: Annotated[int, "Results per page, max 100 (default 100)"] = 100,
) -> str:
    """List all simulation runs across all scenarios in a project.

    Returns a paginated list of runs. Optionally filter by status
    to find active, completed, or failed runs.
    """
    params: dict = {"page": page, "page_size": page_size}
    if status_filter:
        params["status"] = status_filter
    data = await client.get(f"/projects/{project_id}/runs/", params=params)
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# TASK-2469 (W1.1, epic 2467) — project + terrain import tools.
#
# Four thin wrappers over the existing V2 REST API so a cold agent can author a
# project from files it moves ITSELF (decision D7: no tool accepts file
# contents). The presign/finalize pair is the FE's direct-to-S3 path
# (api_v2.py upload_presign / upload_finalize): the bytes never touch uwsgi,
# and no Terrain row exists until finalize. finalize kicks the SAME Celery
# chain as a multipart upload — create_terrain_gn_layer → create_supporting_models —
# which also seeds the project's default Boundary/Friction/Inflow/Rainfall/
# MeshRegion rows that W1.2's attach_input_layer PATCHes.
# ---------------------------------------------------------------------------

# Terrain.status runs creating → styling → ready | error (gn_anuga tasks.py
# :1473/:1542/:1620); only the two terminal states end a poll.
TERRAIN_TERMINAL_STATUSES = frozenset({"ready", "error"})
# Why bounded, and why 240/280: prod's nginx proxies /mcp/ with a 300 s
# proxy_read_timeout (geonode-https.j2). A tool call cut by the proxy returns
# NOTHING to the agent, so a call that stops early, reports `timed_out` with
# the last status and lets the agent re-call is strictly better than a longer
# one. Values above the ceiling are clamped rather than refused.
GET_TERRAIN_DEFAULT_TIMEOUT_SECONDS = 240
GET_TERRAIN_MAX_TIMEOUT_SECONDS = 280


# ---------------------------------------------------------------------------
# Tool 10: create_project
# ---------------------------------------------------------------------------
@mcp.tool
async def create_project(
    name: Annotated[str, "Project name"],
    projection: Annotated[
        str,
        "Working CRS as an EPSG string, normally the UTM zone of the site, "
        "e.g. 'EPSG:32756' (WGS 84 / UTM 56S)",
    ],
) -> str:
    """Create a new ANUGA project. Returns the project record including its id.

    The caller's own account becomes the owner. `projection` is the projected
    CRS every scenario in the project is meshed and run in — pick the UTM zone
    covering the site. Next step for a new project: presign_terrain_upload.
    """
    body, status_code = await client.post(
        "/projects/", json={"name": name, "projection": projection}
    )
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 11: presign_terrain_upload
# ---------------------------------------------------------------------------
@mcp.tool
async def presign_terrain_upload(
    project_id: Annotated[int, "The project ID"],
    filename: Annotated[str, "Base name of the GeoTIFF, e.g. 'dem.tif' (a name only — never contents)"],
    content_type: Annotated[
        str,
        "MIME type the PUT will send (default 'image/tiff'). It is SIGNED into the "
        "URL, so the PUT must send exactly this Content-Type header.",
    ] = "image/tiff",
    size: Annotated[
        int | None,
        "File size in bytes (`stat -c %s <file>`). The API rejects > 5 GiB up "
        "front. Omit if unknown.",
    ] = None,
) -> str:
    """Step 1 of a terrain import: get a presigned S3 PUT URL for a GeoTIFF DEM.

    The agent moves the bytes itself — no tool accepts file contents. After this
    call, PUT the file straight to `upload_url`, sending the SAME Content-Type
    you passed here (it is part of the signature; a mismatch is a 403
    SignatureDoesNotMatch before it is anything else):

        curl -sS -X PUT -H "Content-Type: image/tiff" --upload-file /path/dem.tif "$UPLOAD_URL"

    then call finalize_terrain_upload with the returned staging_key and
    process_id. Nothing exists in Hydrata until finalize; the URL expires after
    `expires_in` seconds (3600).

    Platform guidance: keep the terrain extent within 5° across and 40,000 km²
    (200 × 200 km). The API enforces that cap on its bbox-fetch path; an
    uploaded GeoTIFF is only rejected above 5 GiB, but a larger DEM will not
    mesh or run well. The GeoTIFF must carry a CRS; the import reprojects it to
    the site's UTM zone and builds a hillshade. Fallback when a presigned PUT is
    impossible: multipart-POST the file with the same credential to
    /api/v2/anuga/projects/<id>/terrain/upload/ (form field `file`, optional
    `title`), which creates the terrain in one step — then get_terrain.
    """
    payload: dict = {"filename": filename, "content_type": content_type}
    if size is not None:
        payload["size"] = size
    body, status_code = await client.post(
        f"/projects/{project_id}/terrain/upload/presign/", json=payload
    )
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 12: finalize_terrain_upload
# ---------------------------------------------------------------------------
@mcp.tool
async def finalize_terrain_upload(
    project_id: Annotated[int, "The project ID"],
    staging_key: Annotated[str, "`staging_key` returned by presign_terrain_upload"],
    process_id: Annotated[
        str,
        "`process_id` returned by presign_terrain_upload (pass it so the upload's "
        "progress record is reused; the API tolerates its absence)",
    ] = "",
    title: Annotated[str, "Terrain title (default: the filename without .tif)"] = "",
) -> str:
    """Step 2 of a terrain import: register the PUT GeoTIFF as a Terrain and start the import.

    Call this only after the presigned PUT returned 200. Creates the Terrain row
    (status `creating`) and queues the import chain — reproject to UTM, publish
    the layer + hillshade, style — which also seeds the project's default
    boundary, friction, inflow, rainfall and mesh-region rows. Returns 202 with
    the terrain record; keep its `id` for get_terrain. A 400 UPLOAD_NOT_FOUND
    means no object is at `staging_key`: the PUT did not land (check its status
    code and Content-Type) — do not retry finalize until it has.
    """
    body, status_code = await client.post(
        f"/projects/{project_id}/terrain/upload/finalize/",
        json={"staging_key": staging_key, "process_id": process_id, "title": title},
    )
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 13: get_terrain
# ---------------------------------------------------------------------------
@mcp.tool
async def get_terrain(
    project_id: Annotated[int, "The project ID"],
    terrain_id: Annotated[
        int | None,
        "The terrain ID from finalize_terrain_upload. Omit to follow the "
        "project's newest terrain.",
    ] = None,
    timeout_seconds: Annotated[
        int,
        "How long this call keeps polling before it returns `timed_out` "
        "(default 240, ceiling 280 — the prod /mcp/ proxy cuts a call at 300 s). "
        "0 = one status read, no waiting.",
    ] = GET_TERRAIN_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
) -> str:
    """Poll a terrain's import until it is `ready` (or `error`), bounded by timeout_seconds.

    Returns `outcome` — exactly one of `ready`, `error`, `timed_out`, `not_found`
    — plus the last `status` seen (creating → styling → ready | error), `polls`,
    `elapsed_seconds` and the full `terrain` record (its `gn_layer` is the
    published elevation dataset pk once ready). An import takes minutes (a 32 MB
    1 m DEM: ~4.5 min measured), so `timed_out` is normal: just call again with
    the same arguments. `error` is terminal — the import failed and no default input
    rows were seeded; upload a corrected GeoTIFF as a new terrain.

    `ready` is written by the import task; the project's default input rows
    (Boundary 01, Friction 01, Inflow 01, Rainfall 01, Structure 01,
    MeshRegion 01, one GeoNode layer each) are seeded by the NEXT task in the
    chain and appear over the following ~30 s. Poll the input-layer list until
    it is non-empty before attaching your own layer to a default row.
    """
    timeout_seconds = max(0, min(int(timeout_seconds), GET_TERRAIN_MAX_TIMEOUT_SECONDS))
    poll_interval_seconds = max(0.0, float(poll_interval_seconds))
    if terrain_id is not None:
        path = f"/projects/{project_id}/terrain/{terrain_id}/"
    else:
        path = f"/projects/{project_id}/terrain/"

    started = time.monotonic()
    polls = 0
    terrain_count: int | None = None
    while True:
        data = await client.get(path)
        polls += 1
        if terrain_id is None:
            # GET /terrain/ answers a BARE JSON array (no count/results); follow
            # the newest row, which is the one the caller just finalized.
            rows = data if isinstance(data, list) else data.get("results", [])
            terrain_count = len(rows)
            if not rows:
                terrain, status, outcome = None, None, "not_found"
                break
            terrain = max(rows, key=lambda row: row.get("id", 0))
        else:
            terrain = data
        status = terrain.get("status") if isinstance(terrain, dict) else None
        if status in TERRAIN_TERMINAL_STATUSES:
            outcome = status
            break
        if time.monotonic() - started >= timeout_seconds:
            outcome = "timed_out"
            break
        await asyncio.sleep(poll_interval_seconds)

    result: dict = {
        "outcome": outcome,
        "status": status,
        "polls": polls,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "terrain": terrain,
    }
    if terrain_id is None:
        result["terrain_count"] = terrain_count
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# ASGI app factory + CLI entry point
# ---------------------------------------------------------------------------
def create_app():
    """Create ASGI application for uvicorn."""
    return mcp.http_app(path="/", stateless_http=True, middleware=MIDDLEWARE)


# Module-level ASGI app for `uvicorn hydrata_mcp.server:app`
app = create_app()


def main():
    """CLI entry point: `hydrata-mcp`.

    Runs uvicorn on the identical ``app`` that prod's unit file runs
    (``uvicorn hydrata_mcp.server:app``): one server object, one path (``/``),
    one middleware list. TASK-3181 (W1.0, epic 2467) — fastmcp's own runner
    built a SECOND app at its default path, so the CLI and prod disagreed on
    where the endpoint was (pre-existing since 7586f72, which moved ``create_app``
    to ``/`` only).
    """
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
