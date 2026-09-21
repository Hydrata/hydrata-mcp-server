"""Hydrata MCP Server — 17 hand-crafted tools for ANUGA flood simulation."""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.middleware import Middleware

from .client import HydrataAPIError, HydrataClient
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
        "finalize_terrain_upload → get_terrain (polls until ready) → for each "
        "input GeoJSON (boundary, friction, inflow, rainfall, structure, mesh_region): "
        "multipart-POST it yourself to <origin>/api/v2/uploads/upload/ with the "
        "same credential, then attach_input_layer with the execution_id → "
        "create_time_series for each rain gauge / hydrograph → create_scenario "
        "(reports the mesh-triangle estimate from the detail) → build_scenario "
        "(polls computed_status to built; asks for confirm=true above 100,000 "
        "triangles; returns the API's 422 MESH_TOO_LARGE body verbatim) → "
        "start_simulation. No tool accepts file contents; the agent moves the bytes."
    ),
    lifespan=lifespan,
)


def _post_result(body, status_code: int) -> str:
    """Tool text for a POST: the API body plus `http_status`.

    Some endpoints answer 202 (queued) rather than 200/201, and the agent
    needs to see which; a non-dict body is wrapped as {"response": body}
    so the status key always has a dict to live in.
    """
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Bounded polling — ONE loop for every tool that waits on the platform
# (get_terrain, attach_input_layer, build_scenario).
#
# Why bounded, and why 240/280: prod's nginx proxies /mcp/ with a 300 s
# proxy_read_timeout (geonode-https.j2). A tool call cut by the proxy returns
# NOTHING to the agent, so a call that stops early, reports `timed_out` with
# the last status and lets the agent re-call is strictly better than a longer
# one. Values above the ceiling are clamped rather than refused.
# ---------------------------------------------------------------------------
POLL_DEFAULT_TIMEOUT_SECONDS = 240
POLL_MAX_TIMEOUT_SECONDS = 280


def _status_of(record) -> str | None:
    """`status` of an API record, or None when it is not a dict."""
    return record.get("status") if isinstance(record, dict) else None


async def _poll_until(fetch, is_terminal, timeout_seconds, poll_interval_seconds):
    """Call `fetch()` until `is_terminal(value)` or `timeout_seconds` elapses.

    Returns (last value, polls, elapsed_seconds, timed_out). Always fetches at
    least once (timeout 0 = one read, no wait); a terminal value on the last
    read wins over the bound. The sleep is min(interval, remaining) — the W1a
    round-1 sweep's Tier-A finding: an uncapped interval longer than what was
    left overshot the bound (and prod's 300 s proxy cut).
    """
    timeout_seconds = max(0, min(int(timeout_seconds), POLL_MAX_TIMEOUT_SECONDS))
    poll_interval_seconds = max(0.0, float(poll_interval_seconds))
    started = time.monotonic()
    polls = 0
    while True:
        value = await fetch()
        polls += 1
        if is_terminal(value):
            timed_out = False
            break
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            timed_out = True
            break
        await asyncio.sleep(min(poll_interval_seconds, remaining))
    return value, polls, round(time.monotonic() - started, 1), timed_out


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
    """Get scenario details including its `computed_status` and latest run.

    The `computed_status` field (there is NO `status` key on a scenario) is
    derived from the latest run and will be one of: created, building, built,
    queued, computing, processing, complete, error, or cancelled — `created`
    also means no run exists yet. A scenario must be `built` before it can be
    run. The detail also carries `mesh_triangle_count_estimate` (+ its
    `_breakdown`) and `latest_run_is_valid` (false after any edit since the
    last build). A non-member reading a public project's scenario gets a
    reduced record with no `computed_status`.
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
    return _post_result(body, status_code)


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
    return _post_result(body, status_code)


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
    return _post_result(body, status_code)


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
# which also seeds the project's SIX default Boundary/Friction/Inflow/Rainfall/
# Structure/MeshRegion rows that W1.2's attach_input_layer PATCHes.
# ---------------------------------------------------------------------------

# Terrain.status runs creating → styling → ready | error (gn_anuga tasks.py
# :1473/:1542/:1620); only the two terminal states end a poll.
TERRAIN_TERMINAL_STATUSES = frozenset({"ready", "error"})


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
    return _post_result(body, status_code)


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
    return _post_result(body, status_code)


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
    the layer + hillshade, style — which also seeds the project's six default
    boundary, friction, inflow, rainfall, structure and mesh-region rows.
    Returns 202 with the terrain record; keep its `id` for get_terrain. A 400 UPLOAD_NOT_FOUND
    means no object is at `staging_key`: the PUT did not land (check its status
    code and Content-Type) — do not retry finalize until it has.
    """
    body, status_code = await client.post(
        f"/projects/{project_id}/terrain/upload/finalize/",
        json={"staging_key": staging_key, "process_id": process_id, "title": title},
    )
    return _post_result(body, status_code)


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
    ] = POLL_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
) -> str:
    """Poll a terrain's import until it is `ready` (or `error`), bounded by timeout_seconds.

    Returns `outcome` — exactly one of `ready`, `error`, `timed_out`, `not_found`
    (the project has no terrain yet; an unknown terrain_id is an API 404 error
    instead) — plus the last `status` seen (creating → styling → ready | error),
    `polls`, `elapsed_seconds` and the full `terrain` record (its `gn_layer` is
    the published elevation dataset pk once ready). An import takes minutes to
    tens of minutes (the SAME 32 MB 1 m DEM measured 4.5 min once and 26 min
    once — the worker's S3 download speed dominates), so `timed_out` is normal
    and NOT a failure: keep calling with the same arguments while `status` is
    still `creating`/`styling`; several calls in a row is expected. `error` is
    terminal — the import failed and no default input rows were seeded; upload
    a corrected GeoTIFF as a new terrain.

    `ready` is written by the import task; the project's default input rows
    (Boundary 01, Friction 01, Inflow 01, Rainfall 01, Structure 01,
    MeshRegion 01, one GeoNode layer each) are seeded by the NEXT task in the
    chain and appear over the following ~30 s. Poll the input-layer list until
    it is non-empty before attaching your own layer to a default row.
    """
    if terrain_id is not None:
        path = f"/projects/{project_id}/terrain/{terrain_id}/"
    else:
        path = f"/projects/{project_id}/terrain/"

    async def fetch():
        data = await client.get(path)
        if terrain_id is not None:
            return data, None
        # GET /terrain/ answers a BARE JSON array (no count/results); follow
        # the newest row, which is the one the caller just finalized.
        rows = data if isinstance(data, list) else data.get("results", [])
        newest = max(rows, key=lambda row: row.get("id", 0)) if rows else None
        return newest, len(rows)

    def is_terminal(value):
        terrain, _ = value
        # No terrain at all is `not_found` — terminal, nothing to wait for.
        return terrain is None or _status_of(terrain) in TERRAIN_TERMINAL_STATUSES

    (terrain, terrain_count), polls, elapsed, timed_out = await _poll_until(
        fetch, is_terminal, timeout_seconds, poll_interval_seconds
    )
    status = _status_of(terrain)
    if terrain is None:
        outcome = "not_found"
    elif timed_out:
        outcome = "timed_out"
    else:
        outcome = status
    result: dict = {
        "outcome": outcome,
        "status": status,
        "polls": polls,
        "elapsed_seconds": elapsed,
        "terrain": terrain,
    }
    if terrain_id is None:
        result["terrain_count"] = terrain_count
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# TASK-3171 (W1.2, epic 2467) — time series + input-layer attach tools.
#
# create_time_series is the one tool that takes structured data inline: a
# gauge is a few thousand {timestamp, value} rows (~72 KB compact for the
# largest Towradgi gauge), not a file — decision D7 forbids BYTES, and the
# GeoJSONs stay with the agent. attach_input_layer never sees the GeoJSON
# either: the agent multipart-POSTs it to GeoNode's /api/v2/uploads/upload/
# with its own credential and hands over the execution id; the tool follows
# the import, then PATCHes the dataset pk onto the project's DEFAULT row.
# ---------------------------------------------------------------------------

# TimeSeries.series_type choices (hydrology/models.py:90-95). The list route
# filters by ?series_type and the Hydrographs panel reads `hydrograph` rows,
# so the value must travel as a TOP-LEVEL key — folded into `description` it
# would leave an inflow series invisible. Validated here so an unknown value
# is refused without an upstream call.
SERIES_TYPES = ("hyetograph", "hydrograph", "stage", "generic")

# kind → the plural V2 route (gn_anuga urls.py :141-:225). ALL SIX are handled
# the same way — GET the list, PATCH the default row's gn_layer — because the
# terrain chain seeds all six (tasks.py create_supporting_models :2802-2809)
# and a POST to /structures/ or /mesh-regions/ carrying gn_layer is silently
# OVERWRITTEN: perform_create (api_v2.py :5247-5261) unconditionally queues
# create_layer_for_model, which makes an empty dataset and re-saves gn_layer
# moments after the 201.
INPUT_LAYER_ROUTES = {
    "boundary": "boundaries",
    "friction": "frictions",
    "inflow": "inflows",
    "rainfall": "rainfalls",
    "structure": "structures",
    "mesh_region": "mesh-regions",
}
# The title create_supporting_models gives each default row; used to pick the
# row deterministically when a project has more than one (the list has no
# declared ordering).
INPUT_LAYER_DEFAULT_TITLES = {
    "boundary": "Boundary 01",
    "friction": "Friction 01",
    "inflow": "Inflow 01",
    "rainfall": "Rainfall 01",
    "structure": "Structure 01",
    "mesh_region": "MeshRegion 01",
}
# No REST create path exists for either at HEAD (api_v2.py:3392: "no create
# path exists today (TASK-3040 AC6)"), and run_anuga does not convey culvert
# flow — so the tool refuses up front rather than pretending the layer landed.
UNSUPPORTED_INPUT_LAYER_KINDS = frozenset({"breakline", "culvert"})
UNSUPPORTED_INPUT_LAYER_REASON = (
    "no REST create path exists for breaklines or culverts at HEAD (TASK-3040 AC6 "
    "retired the UI surface; the models + Scenario FKs stay dormant), and culvert "
    "flow is not conveyed by run_anuga — attaching one would only pretend the "
    "layer landed"
)
# GeoNode ExecutionRequest statuses (geonode/resource/models.py:29-32).
EXECUTION_TERMINAL_STATUSES = frozenset({"finished", "failed"})


def _validate_row_data(data) -> int:
    """Client-side mirror of TimeSeries.clean() (hydrology/models.py:126-151).

    Returns the row count. Why here: save() runs full_clean(), whose
    ValidationError is NOT a DRF APIException, so a bad row (or a `data` that
    is not {"rowData": [...]}) is an UNHANDLED 500 whose body the client masks
    — the agent would see "server error" for a typo in one timestamp. The
    checks are exactly clean()'s: every row a dict with `timestamp` (ISO 8601
    string, trailing Z tolerated) and `value` (float()-able). Rows are NOT
    rewritten — clean() normalises them server-side.
    """
    if not isinstance(data, dict) or not isinstance(data.get("rowData"), list):
        raise ToolError(
            "data must be an object shaped {\"rowData\": [{\"timestamp\": \"<ISO 8601>\", "
            "\"value\": <number>}, ...]} — the API answers 500, not 400, to any other shape"
        )
    for index, row in enumerate(data["rowData"]):
        if not isinstance(row, dict) or not {"timestamp", "value"} <= set(row):
            raise ToolError(
                f"data.rowData row {index} must be an object with 'timestamp' and 'value' keys"
            )
        timestamp = row["timestamp"]
        if not isinstance(timestamp, str):
            raise ToolError(f"data.rowData row {index}: timestamp must be an ISO 8601 string")
        try:
            datetime.fromisoformat(timestamp.rstrip("Z"))
            float(row["value"])
        except (TypeError, ValueError):
            raise ToolError(
                f"data.rowData row {index}: timestamp must be ISO 8601 and value numeric "
                f"(got timestamp={timestamp!r}, value={row['value']!r})"
            )
    return len(data["rowData"])


# ---------------------------------------------------------------------------
# Tool 14: create_time_series
# ---------------------------------------------------------------------------
@mcp.tool
async def create_time_series(
    project_id: Annotated[int, "The project ID"],
    name: Annotated[
        str,
        "Series name, kept VERBATIM — a rainfall polygon binds to its gauge by this "
        "exact name (its `data` property), so use the name the GeoJSON features carry",
    ],
    data: Annotated[
        dict,
        "The rows: {\"rowData\": [{\"timestamp\": \"1998-08-17T00:00:00\", \"value\": 1.5}, "
        "...]}. Timestamps ISO 8601 (a trailing Z is tolerated), values numeric. "
        "Validated here before anything is sent.",
    ],
    description: Annotated[str, "Free-text description (optional)"] = "",
    series_type: Annotated[
        str,
        "One of hyetograph (rainfall depth/intensity — the default), hydrograph "
        "(flow, read by the Hydrographs panel and inflow boundaries), stage (water "
        "level, e.g. a tide) or generic. Sent as a top-level field.",
    ] = "hyetograph",
    units: Annotated[str, "Display label for the values, e.g. 'mm/hr', 'm^3/s', 'm' (optional)"] = "",
    source: Annotated[str, "Where the data came from, e.g. a gauge id or agency (optional)"] = "",
    location_name: Annotated[str, "Human-readable location of the gauge (optional)"] = "",
    timezone: Annotated[str, "IANA timezone of the timestamps (default 'UTC'), e.g. 'Australia/Sydney'"] = "UTC",
) -> str:
    """Create a time series (rain gauge, hydrograph, tide/stage) in a project.

    POSTs /projects/<id>/time-series/ with `series_type` and `units` as
    top-level fields. `series_type` is checked against the four choices and
    `data` against the {"rowData": [{timestamp, value}, ...]} shape before any
    request is made — the API answers 500 (not 400) to a malformed row.
    Returns a compact record — id, name, series_type, units, timezone,
    row_count, http_status — not the echoed rows; fetch the full row with the
    REST API (GET /projects/<id>/time-series/<id>/) if you need to round-trip it.
    A rainfall polygon references its gauge by the series NAME, so create the
    gauges with the exact names the rainfall GeoJSON's features carry.
    """
    if series_type not in SERIES_TYPES:
        raise ToolError(
            f"series_type {series_type!r} is not one of {', '.join(SERIES_TYPES)}"
        )
    row_count = _validate_row_data(data)
    payload = {
        "name": name,
        "description": description,
        "source": source,
        "location_name": location_name,
        "timezone": timezone,
        "series_type": series_type,
        "units": units,
        "data": data,
    }
    body, status_code = await client.post(f"/projects/{project_id}/time-series/", json=payload)
    if not isinstance(body, dict):
        return _post_result(body, status_code)
    landed = body.get("data")
    if isinstance(landed, dict) and isinstance(landed.get("rowData"), list):
        row_count = len(landed["rowData"])
    return json.dumps(
        {
            "id": body.get("id"),
            "name": body.get("name", name),
            "series_type": body.get("series_type", series_type),
            "units": body.get("units", units),
            "timezone": body.get("timezone", timezone),
            "row_count": row_count,
            "http_status": status_code,
        },
        indent=2,
    )


def _first_resource_id(record) -> int | str | None:
    """`output_params.resources[0].id` of an execution record, or None."""
    if not isinstance(record, dict):
        return None
    resources = (record.get("output_params") or {}).get("resources") or []
    if not resources or not isinstance(resources[0], dict):
        return None
    pk = resources[0].get("id")
    if isinstance(pk, str) and pk.isdigit():
        return int(pk)
    return pk


def _pick_default_row(rows: list, kind: str) -> dict:
    """The '<Kind> 01' row if present, else the lowest id — the list has no declared ordering."""
    for row in rows:
        if isinstance(row, dict) and row.get("title") == INPUT_LAYER_DEFAULT_TITLES[kind]:
            return row
    return min(rows, key=lambda row: row.get("id", 0) if isinstance(row, dict) else 0)


# ---------------------------------------------------------------------------
# Tool 15: attach_input_layer
# ---------------------------------------------------------------------------
@mcp.tool
async def attach_input_layer(
    project_id: Annotated[int, "The project ID"],
    kind: Annotated[
        str,
        "Which input the uploaded layer is: boundary, friction, inflow, rainfall, "
        "structure or mesh_region. breakline and culvert are refused (see below).",
    ],
    execution_id: Annotated[
        str,
        "`execution_id` returned by your multipart POST to <origin>/api/v2/uploads/upload/",
    ],
    timeout_seconds: Annotated[
        int,
        "How long this call keeps polling the upload before it returns `timed_out` "
        "(default 240, ceiling 280 — the prod /mcp/ proxy cuts a call at 300 s). "
        "0 = one status read, no waiting.",
    ] = POLL_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
) -> str:
    """Attach a GeoJSON you uploaded to GeoNode as the project's boundary, friction, inflow, rainfall, structure or mesh_region layer.

    The agent moves the bytes: first upload the GeoJSON yourself, with the
    same credential, to GeoNode's upload endpoint at the site origin:

        curl -sS -u <user>:<password> -F "base_file=@/path/rainfall.geojson" https://<site>/api/v2/uploads/upload/

    → JSON with `execution_id`. Then call this tool with it. The tool polls
    GET /api/v2/resource-service/execution-status/<execution_id> (bounded by
    timeout_seconds; statuses ready → running → finished | failed), reads the
    new dataset's pk, and PATCHes it onto the project's DEFAULT row of that
    kind ('Boundary 01' … 'MeshRegion 01' — the six rows the terrain import
    seeds ~30 s after get_terrain reports ready; if the list is still empty
    the tool says so: run finalize_terrain_upload / wait for get_terrain first).

    `outcome` is exactly one of: `attached` (row_id, gn_layer, dataset_pk,
    dataset_alternate — the WFS typename — and every row id seen);
    `timed_out` (the import is still running: call again with the same
    arguments); `upload_failed` (the execution record, incl. its log, is
    returned verbatim — fix the file and upload again); `no_default_row`;
    `no_dataset` (finished but no resource was recorded). The PATCH is
    refused with a 400 if the dataset is not owned by you.

    Refused, with no request made: kind `breakline` and kind `culvert` — no
    REST create path exists for either at HEAD (TASK-3040 AC6), and culvert
    flow is not conveyed by run_anuga, so attaching one would only pretend.
    Also refused without a request: an execution_id that is not the UUID the
    upload returned.
    One GeoJSON per kind: all of a kind's features travel in that one file. A
    rainfall polygon names its gauge in its `data` property — create that
    gauge with create_time_series under the exact same name.
    """
    kind = kind.strip().lower().replace("-", "_")
    if kind in UNSUPPORTED_INPUT_LAYER_KINDS:
        raise ToolError(
            f"kind {kind!r} is refused: {UNSUPPORTED_INPUT_LAYER_REASON}. "
            f"Accepted kinds: {', '.join(INPUT_LAYER_ROUTES)}."
        )
    if kind not in INPUT_LAYER_ROUTES:
        raise ToolError(
            f"kind {kind!r} is not one of {', '.join(INPUT_LAYER_ROUTES)} "
            "(breakline and culvert are refused: no REST create path at HEAD, TASK-3040 AC6)"
        )
    route = INPUT_LAYER_ROUTES[kind]
    # The one free-form path segment a tool interpolates into a URL — and on
    # prod that URL is the unthrottled loopback door to Django (geonode-https.j2
    # :8081). GeoNode's exec_id is a UUIDField (geonode/resource/models.py), so
    # anything else is refused here, before a request is built; the canonical
    # form is what both GeoNode routes below match.
    try:
        execution_id = str(uuid.UUID(str(execution_id)))
    except ValueError:
        raise ToolError(
            f"execution_id {execution_id!r} is not a UUID — pass the `execution_id` "
            "your upload POST to /api/v2/uploads/upload/ returned, verbatim"
        )
    # GeoNode's route, at the ORIGIN, with NO trailing slash
    # (geonode/resource/api/urls.py:26) — 404 with one.
    status_path = f"/api/v2/resource-service/execution-status/{execution_id}"

    execution, polls, elapsed, timed_out = await _poll_until(
        lambda: client.get_from_origin(status_path),
        lambda record: _status_of(record) in EXECUTION_TERMINAL_STATUSES,
        timeout_seconds,
        poll_interval_seconds,
    )
    status = _status_of(execution)
    if timed_out:
        return json.dumps(
            {
                "outcome": "timed_out",
                "kind": kind,
                "execution_status": status,
                "polls": polls,
                "elapsed_seconds": elapsed,
            },
            indent=2,
        )

    result: dict = {
        "outcome": None,
        "kind": kind,
        "route": route,
        "execution_status": status,
        "polls": polls,
        "elapsed_seconds": elapsed,
    }
    if status == "failed":
        result["outcome"] = "upload_failed"
        result["execution"] = execution  # verbatim: the log names the import error
        return json.dumps(result, indent=2)

    dataset_pk = _first_resource_id(execution)
    if dataset_pk is None:
        # The e2e recipe's fallback (test_anuga_merewether.py:296-320): the
        # detail route carries the same output_params under `request`.
        detail = await client.get_from_origin(f"/api/v2/executionrequest/{execution_id}/")
        dataset_pk = _first_resource_id(detail.get("request") if isinstance(detail, dict) else None)
    result["dataset_pk"] = dataset_pk
    if dataset_pk is None:
        result["outcome"] = "no_dataset"
        result["execution"] = execution
        return json.dumps(result, indent=2)

    listing = await client.get(f"/projects/{project_id}/{route}/")
    # A PLAIN JSON list on the six input routes (no count/results wrapper).
    rows = listing if isinstance(listing, list) else listing.get("results", [])
    result["row_ids"] = [row.get("id") for row in rows if isinstance(row, dict)]
    if not rows:
        result["outcome"] = "no_default_row"
        result["message"] = (
            f"project {project_id} has no {kind} row to attach to. The default "
            f"'{INPUT_LAYER_DEFAULT_TITLES[kind]}' row is seeded by the terrain import: run "
            "finalize_terrain_upload, wait for get_terrain to report ready, then ~30 s more."
        )
        return json.dumps(result, indent=2)

    row = _pick_default_row(rows, kind)
    body, status_code = await client.patch(
        f"/projects/{project_id}/{route}/{row['id']}/", json={"gn_layer": dataset_pk}
    )
    result.update(
        {
            "outcome": "attached",
            "row_id": row["id"],
            "row_title": row.get("title"),
            "gn_layer": body.get("gn_layer") if isinstance(body, dict) else None,
            "http_status": status_code,
        }
    )
    # The WFS typename, so the agent can count features without a GeoNode
    # tool of its own. Best effort: the attach already happened.
    try:
        detail = await client.get_from_origin(f"/api/v2/datasets/{dataset_pk}/")
        result["dataset_alternate"] = (detail.get("dataset") or {}).get("alternate")
    except HydrataAPIError as exc:
        result["dataset_alternate"] = None
        result["dataset_lookup_error"] = str(exc)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# TASK-3172 (W1.3, epic 2467) — create_scenario + build_scenario.
#
# Building is the step that costs (make_package meshes the boundary on the web
# box; the 2026-09-06 outage was a 28.7M-triangle mesh built in-process), so
# the tool shows the number before it spends: create_scenario reports the
# detail's mesh_triangle_count_estimate, and build_scenario refuses above
# 100,000 triangles unless the agent passes confirm=true — a client-side
# courtesy gate, deliberately BELOW the server's own 422 MESH_TOO_LARGE
# ceiling (estimate.py build_size_refusal; 7.7M at the 28 GiB default), which
# stays the hard limit and whose body the tool returns verbatim.
# ---------------------------------------------------------------------------

# Above this estimate build_scenario wants confirm=true. Courtesy, not the
# ceiling: the server refuses only above gpu_l40s_max_triangles (422).
BUILD_CONFIRM_ABOVE_TRIANGLES = 100_000

# RunState values (gn_anuga state_machine.py:16-26), as the detail's
# computed_status echoes them (= latest_run.status, or `created` with no run).
#
# A run in one of these is being built right now (or is about to be): the
# server's dedup 409 (BUILD_DEDUP_BLOCKING_STATUS_VALUES, api_v2.py:222) covers
# them, so the tool resumes polling instead of POSTing. `created` is both "no
# run yet" and "dispatched, no worker has picked it up".
BUILD_IN_FLIGHT_RUN_STATUSES = frozenset({"created", "building"})
# A run in one of these HAS a package. A re-POST is not dedup-blocked for
# `built` or `complete` (services.py:1042-1048 dispatches a NEW Run), so the
# tool returns the state instead — unless rebuild=true, or latest_run_is_valid
# is false (every PATCH sets it false, api_v2.py:3095-3101: the package is
# stale against the scenario's current inputs).
BUILD_DONE_RUN_STATUSES = frozenset({"built", "queued", "computing", "processing", "complete"})
# Where a build poll stops: a package exists (the four simulation states mean a
# run already went past built — a re-call after start_simulation — and
# `complete` is a finished run; none is worth waiting on) or the run died.
# Everything in flight is, by construction, NOT terminal.
BUILD_TERMINAL_STATUSES = BUILD_DONE_RUN_STATUSES | {"error", "cancelled"}


def _computed_status(scenario) -> str | None:
    """`computed_status` of a scenario detail, or None when it is not a dict.

    Never `status`: ScenarioSerializerV2 has no such key (the round-2 red-team
    caught the pre-existing get_scenario test mocking one).
    """
    return scenario.get("computed_status") if isinstance(scenario, dict) else None


def _build_state(detail, **extra) -> dict:
    """The compact build state: the scenario's status + estimate and the latest
    run's id/status/error/mesh count — never the whole detail (`latest_run`
    carries the full build log; the agent has get_run for that)."""
    # A non-dict detail (or latest_run) reads as empty: every field is None.
    detail = detail if isinstance(detail, dict) else {}
    run = detail.get("latest_run")
    run = run if isinstance(run, dict) else {}
    state = {
        "scenario_id": detail.get("id"),
        "computed_status": _computed_status(detail),
        "mesh_triangle_count_estimate": detail.get("mesh_triangle_count_estimate"),
        "latest_run_is_valid": detail.get("latest_run_is_valid"),
        "run_id": run.get("id"),
        "run_status": _status_of(run),
        "error_message": run.get("error_message"),
        "user_message": run.get("user_message"),
        "status_detail": run.get("status_detail"),
        "mesh_triangle_count": run.get("mesh_triangle_count"),
    }
    state.update(extra)
    return state


def _build_refusal(detail, reason: str) -> str:
    return json.dumps(
        _build_state(detail, outcome="refused", posted=False, reason=reason), indent=2
    )


# ---------------------------------------------------------------------------
# Tool 16: create_scenario
# ---------------------------------------------------------------------------
@mcp.tool
async def create_scenario(
    project_id: Annotated[int, "The project ID"],
    name: Annotated[str, "Scenario name"],
    resolution: Annotated[
        float,
        "Mesh resolution as a LENGTH in metres (ANUGA maximum_triangle_area = "
        "resolution²/2). Coarser (larger) = fewer triangles: on a ~13 km² boundary "
        "36-40 m gives ~16-20k triangles, 10 m ~255k, 1 m ~25M (refused by the API).",
    ],
    duration: Annotated[int, "Simulation duration in seconds (e.g. 43200 = 12 h)"],
    terrain: Annotated[
        int | None, "Terrain id (get_terrain → terrain.id); the build fails without one"
    ] = None,
    boundary: Annotated[
        int | None, "Boundary row id (attach_input_layer kind=boundary → row_id)"
    ] = None,
    friction: Annotated[int | None, "Friction row id (optional)"] = None,
    inflow: Annotated[
        int | None, "Inflow row id (optional; the build needs an inflow OR a rainfall)"
    ] = None,
    rainfall: Annotated[
        int | None, "Rainfall row id (optional; the build needs an inflow OR a rainfall)"
    ] = None,
    structure: Annotated[int | None, "Structure row id (optional)"] = None,
    mesh_region: Annotated[
        int | None,
        "MeshRegion row id (optional). Leave UNSET unless you want the regions' "
        "finer meshing — see the units note: they mesh ~15x more than estimated.",
    ] = None,
    description: Annotated[str, "Free-text description (optional)"] = "",
) -> str:
    """Create a DRAFT scenario (no build, no run) and report its mesh-triangle estimate.

    POSTs /projects/<id>/scenarios/ with the write fields, then GETs the
    scenario detail — the create response carries NO estimate; the detail's
    `mesh_triangle_count_estimate` (+ `_breakdown`) does. Returns a compact
    record: id, name, the FK ids as stored, resolution, duration,
    `computed_status` (`created` = no run yet), the estimate and its
    breakdown, and http_status. Next step: build_scenario (which asks for
    confirm=true above 100,000 triangles). Nothing is meshed or queued here.

    Units: `resolution` on the SCENARIO is a LENGTH in metres — ANUGA
    maximum_triangle_area = resolution²/2 (run_utils.py:227/:290; FloatField
    default 100, not nullable; 0 makes the estimate None) and the same value
    becomes the raster cell size; a MeshRegion FEATURE's `resolution` is
    consumed by the mesher as an AREA (max_triangle_area, m²) while the
    estimate prices it as a length, so attached regions mesh ~15x more than
    estimated (Towradgi 100/36/8 m² regions: estimate ~18k, mesh ~283k); the
    smallest MeshRegion value becomes the raster cell size.

    The server does NOT validate that the FK ids belong to `project_id` —
    pass the row ids attach_input_layer / get_terrain returned for THIS
    project. Every FK is nullable at create time, and the build fails later
    on a scenario with no terrain, or with neither an inflow nor a rainfall.
    A `boundary` whose row has no features (the default 'Boundary 01' before
    attach_input_layer) is accepted here and refused by build_scenario. The
    estimate is None when resolution is 0; an estimate of 0 with a real
    boundary is a very coarse mesh, not an error.
    """
    payload = {
        "name": name,
        "description": description,
        "resolution": resolution,
        "duration": duration,
        "terrain": terrain,
        "boundary": boundary,
        "friction": friction,
        "inflow": inflow,
        "rainfall": rainfall,
        "structure": structure,
        "mesh_region": mesh_region,
    }
    body, status_code = await client.post(f"/projects/{project_id}/scenarios/", json=payload)
    scenario_id = body.get("id") if isinstance(body, dict) else None
    if scenario_id is None:
        return _post_result(body, status_code)
    detail = await client.get(f"/projects/{project_id}/scenarios/{scenario_id}/")
    if not isinstance(detail, dict):
        return _post_result(body, status_code)
    keys = (
        "id", "project", "name", "description", "terrain", "boundary", "friction", "inflow",
        "rainfall", "structure", "mesh_region", "resolution", "duration", "computed_status",
        "mesh_triangle_count_estimate", "mesh_triangle_count_estimate_breakdown",
        "latest_run_is_valid",
    )
    result = {key: detail.get(key) for key in keys}
    result["http_status"] = status_code
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 17: build_scenario
# ---------------------------------------------------------------------------
@mcp.tool
async def build_scenario(
    project_id: Annotated[int, "The project ID"],
    scenario_id: Annotated[int, "The scenario ID from create_scenario"],
    confirm: Annotated[
        bool,
        "Required (true) when the estimate is above 100,000 triangles — the tool "
        "refuses first and tells you the number. Default false.",
    ] = False,
    rebuild: Annotated[
        bool,
        "true to dispatch a NEW build of a scenario whose latest run is already "
        "built/complete and still valid (default false: the tool returns that state "
        "instead of duplicating the build).",
    ] = False,
    timeout_seconds: Annotated[
        int,
        "How long this call keeps polling before it returns `timed_out` "
        "(default 240, ceiling 280 — the prod /mcp/ proxy cuts a call at 300 s). "
        "0 = one status read, no waiting.",
    ] = POLL_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
) -> str:
    """Build a scenario's package (mesh + inputs) after showing what it will cost, and poll until built.

    Order of operations, so the number is shown before anything is spent:

    1. GET the scenario detail. A record with no `boundary`/`computed_status`
       keys is a non-member's read of a public project → refused: a build
       needs the EDITOR role.
    2. Re-call checks on `latest_run` (a re-POST is NOT deduplicated after
       `built`/`complete` — it would dispatch a duplicate build): no run →
       proceed; run `created`/`building` → resume polling, no POST; run
       `built`/`queued`/`computing`/`processing`/`complete` with
       `latest_run_is_valid` not false → return that state, no POST unless
       rebuild=true; run `error`/`cancelled`, or `latest_run_is_valid` false
       (the scenario was edited since the build) → proceed.
    3. Spend gates, each a refusal with `outcome: "refused"` and no POST: the
       scenario has no boundary or its boundary row has no features (the
       server would admit the build and fail it in make_package); the
       estimate is None (resolution is 0/unset); the estimate is above
       100,000 triangles and confirm is not true — the refusal states the
       number; re-call with confirm=true to proceed. An estimate of 0 over a
       boundary WITH features is buildable and is reported, not refused.
    4. POST /projects/<id>/scenarios/<pk>/build/. Every 4xx comes back
       verbatim as a normal result with `http_status`: 422 MESH_TOO_LARGE
       (`error_code`, `estimate`, `ceiling`, `detail` — the server's hard
       ceiling; coarsen `resolution` or shrink the boundary; no run was
       created), 409 COMPUTE_TARGET_UNAVAILABLE, 400/403/404. A 409 WITHOUT
       an error_code is the dedup body ({status, run_id, detail}: a build is
       already in flight) — the tool resumes polling that run.
    5. On 202 poll the detail's `computed_status` (created → building →
       built | error), bounded by timeout_seconds.

    Returns a COMPACT state, never the whole detail: `outcome` (built, error,
    cancelled, complete/queued/computing/processing for a run past the build,
    timed_out, or refused), `computed_status`, `mesh_triangle_count_estimate`,
    `posted`, the POST's `build` body + `http_status` when one was made, and
    the latest run's `run_id`, `run_status`, `error_message`, `user_message`,
    `mesh_triangle_count`. `timed_out` is normal (make_package re-downloads
    the terrain from S3 every build; minutes): call again with the same
    arguments — step 2 resumes polling the same run, it never POSTs twice —
    or cancel_run(run_id) if the run is stuck in `created` with no worker.
    `error` is terminal: `error_message` says why (fix the inputs, then call
    again — an errored latest run is rebuilt).

    Units: `resolution` on the SCENARIO is a LENGTH in metres — ANUGA
    maximum_triangle_area = resolution²/2 (run_utils.py:227/:290; FloatField
    default 100, not nullable; 0 makes the estimate None) and the same value
    becomes the raster cell size; a MeshRegion FEATURE's `resolution` is
    consumed by the mesher as an AREA (max_triangle_area, m²) while the
    estimate prices it as a length, so attached regions mesh ~15x more than
    estimated (Towradgi 100/36/8 m² regions: estimate ~18k, mesh ~283k); the
    smallest MeshRegion value becomes the raster cell size.
    """
    path = f"/projects/{project_id}/scenarios/{scenario_id}/"
    detail = await client.get(path)
    if not isinstance(detail, dict) or "boundary" not in detail or "computed_status" not in detail:
        # STRANGER_SCENARIO_FIELDS (serializers_v2.py:395-405): a public
        # project's scenario read by a non-member has neither key.
        return _build_refusal(
            detail,
            f"not a project member; build needs EDITOR on project {project_id} "
            "(the scenario read carries no boundary/computed_status)",
        )

    async def poll(**extra):
        last, polls, elapsed, timed_out = await _poll_until(
            lambda: client.get(path),
            lambda record: _computed_status(record) in BUILD_TERMINAL_STATUSES,
            timeout_seconds,
            poll_interval_seconds,
        )
        state = _build_state(last, polls=polls, elapsed_seconds=elapsed, **extra)
        if timed_out:
            state["outcome"] = "timed_out"
            state["note"] = (
                "still building: call build_scenario again with the same arguments to "
                "resume polling this run (it will not POST a second build); if run_id "
                f"{state['run_id']} stays in `created` no worker has picked it up — "
                "cancel_run(run_id) releases it"
            )
        else:
            state["outcome"] = state["computed_status"]
        return json.dumps(state, indent=2)

    # (2) Re-call checks BEFORE the spend gates.
    latest_run = detail.get("latest_run")
    run_status = _status_of(latest_run)
    if run_status in BUILD_IN_FLIGHT_RUN_STATUSES:
        return await poll(posted=False)
    package_is_current = detail.get("latest_run_is_valid") is not False
    if run_status in BUILD_DONE_RUN_STATUSES and package_is_current and not rebuild:
        state = _build_state(detail, outcome=_computed_status(detail), posted=False)
        state["note"] = (
            f"latest run {state['run_id']} is {run_status} and latest_run_is_valid is not "
            "false — the package is current, nothing was posted; pass rebuild=true to "
            "dispatch a NEW build"
        )
        return json.dumps(state, indent=2)

    # (3) Spend gates.
    boundary_id = detail.get("boundary")
    if boundary_id is None:
        return _build_refusal(
            detail, "boundary has no features: the scenario has no boundary attached "
            "(attach_input_layer kind=boundary, then set it on the scenario)"
        )
    boundary = await client.get(f"/projects/{project_id}/boundaries/{boundary_id}/")
    if not (isinstance(boundary, dict) and boundary.get("has_features")):
        return _build_refusal(
            detail, f"boundary has no features: Boundary {boundary_id} has_features is false "
            "(the server would admit the build and fail it in make_package) — upload the "
            "boundary GeoJSON and attach_input_layer kind=boundary first"
        )
    estimate = detail.get("mesh_triangle_count_estimate")
    if estimate is None:
        return _build_refusal(detail, "resolution is 0/unset: the mesh estimate is None")
    if estimate > BUILD_CONFIRM_ABOVE_TRIANGLES and not confirm:
        return _build_refusal(
            detail,
            f"estimate is {estimate:,} triangles, above {BUILD_CONFIRM_ABOVE_TRIANGLES:,}: "
            "building costs minutes on the web box and the run will need compute — "
            "coarsen `resolution` (a length in metres; triangles scale ~1/resolution²) "
            "or call again with confirm=true to build at this size",
        )

    # (4) POST; every 4xx comes back as a result the agent can read.
    body, status_code = await client.post(
        f"/projects/{project_id}/scenarios/{scenario_id}/build/", raise_for_status=False
    )
    is_dedup_409 = status_code == 409 and isinstance(body, dict) and "error_code" not in body
    if status_code >= 400 and not is_dedup_409:
        return _post_result(body, status_code)
    # (5) 202 (a new run) or the dedup 409 (someone else's run): poll it.
    return await poll(posted=True, build=body, http_status=status_code)


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
