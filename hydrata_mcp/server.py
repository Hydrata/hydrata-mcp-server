"""Hydrata MCP Server — 28 hand-crafted tools for ANUGA flood simulation."""

import asyncio
import functools
import json
import math
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from urllib.parse import quote, urlencode

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp_types import ToolAnnotations
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
        "To see what a project already holds, use list_inputs and list_time_series. "
        "A typical workflow is: list_projects → get_scenario → start_simulation → "
        "poll get_run_status until complete → get_run for results. "
        "To author a project from your own files: create_project → "
        "presign_terrain_upload → PUT the GeoTIFF yourself (curl --upload-file) → "
        "finalize_terrain_upload → get_terrain (polls until ready) → for each "
        "input GeoJSON (boundary, friction, inflow, rainfall, building, mesh_region, culvert): "
        "multipart-POST it yourself to <origin>/api/v2/uploads/upload/ with the "
        "same credential, then attach_input_layer with the execution_id → "
        "create_time_series for each rain gauge / hydrograph → create_scenario "
        "(reports the mesh-triangle estimate from the detail) → build_scenario "
        "(polls computed_status to built; asks for confirm=true above 100,000 "
        "triangles; returns the API's 422 MESH_TOO_LARGE body verbatim) → "
        "start_simulation. No tool accepts file contents; the agent moves the bytes. "
        "THE HUMAN SIGNS OFF: every tool that changes something returns `ui_url` (a "
        "link that opens the project map on the thing that changed), a `summary` of "
        "numbers the engineer can check, and `say_to_user` (what to tell them). Relay "
        "say_to_user and the ui_url to the user after each layer lands. Before "
        "start_simulation (the step that spends compute), give the user the ui_url and "
        "WAIT for them to confirm they have reviewed the inputs in the map; do not "
        "start a run on your own say-so. When a tool refuses because an input is "
        "missing or wrong (no boundary, no External boundary edge), send the user to "
        "the ui_url to draw or fix it in the map. "
        "To record real flood evidence (Flood marks, Timed levels, Gauge records, Dry "
        "points) that a historic run is compared with (Compare with observations): "
        "create_observation_layer → add_observations → list_observations; edit with "
        "update_observation / delete_observation. Photos go on by link only "
        "(add_observation_photo_link); photo files are uploaded by the engineer in the "
        "map's photo strip. "
        "get_observation_comparison reads how a historic run compares with them (there is no "
        "pass or fail: report the headline); set_observation_exclusion excludes one with the "
        "engineer's reason (ask for it, never invent one) or includes it again; "
        "set_comparison_tolerance saves the project's default tolerance (an Editor's choice: "
        "to try another, pass tolerance to get_observation_comparison instead)."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# TASK-3204 (W1, epic 3200) — ToolAnnotations on every tool. Hints only:
# Claude Code's permission engine ignores them today, but hosted surfaces use
# readOnlyHint for "Allow always". destructiveHint defaults to TRUE in the
# spec, so every write that is not cancel_run says false explicitly.
# (title, read_only, destructive, idempotent)
# ---------------------------------------------------------------------------
TOOL_ANNOTATIONS = {
    "list_projects": ("List projects", True, False, True),
    "get_project": ("Get a project and its map link", True, False, True),
    "get_scenario": ("Get a scenario's state, inputs and price", True, False, True),
    "get_run_status": ("Poll a run's status", True, False, True),
    "get_run": ("Get a run's full record", True, False, True),
    "list_runs": ("List a project's runs", True, False, True),
    "get_terrain": ("Wait for a terrain import", True, False, True),
    "list_inputs": ("List a project's input layers", True, False, True),
    "list_time_series": ("List a project's time series", True, False, True),
    "start_simulation": ("Start a simulation run (spends compute)", False, False, False),
    "cancel_run": ("Cancel a run", False, True, True),
    "retry_run": ("Retry a failed run", False, False, False),
    "create_project": ("Create a project", False, False, False),
    "presign_terrain_upload": ("Get a terrain upload URL", False, False, False),
    "finalize_terrain_upload": ("Register an uploaded terrain", False, False, False),
    "create_time_series": ("Create a time series", False, False, False),
    # TASK-3641: not idempotent — a culvert attach re-uploads its mapped culverts.
    "attach_input_layer": ("Attach an uploaded layer as a project input", False, False, False),
    "create_scenario": ("Create a draft scenario", False, False, False),
    "build_scenario": ("Build a scenario's mesh and package", False, False, False),
    # TASK-3812 (epic 3801): flood observations. delete_observation is the second
    # destructive tool (a feature and its photos go for good).
    "create_observation_layer": ("Create a flood observation layer", False, False, False),
    "list_observations": ("List flood observation layers or observations", True, False, True),
    "add_observations": ("Add flood observations to a layer", False, False, False),
    "update_observation": ("Edit one flood observation", False, False, True),
    "delete_observation": ("Delete one flood observation", False, True, True),
    "add_observation_photo_link": ("Attach a photo link to a flood observation", False, False, False),
    # TASK-3816 / TASK-3844 (epic 3801): Compare with observations. Exclusions
    # are logged and reversible (include it again), so not destructive.
    "get_observation_comparison": ("Compare a run with flood observations", True, False, True),
    "set_observation_exclusion": ("Exclude or include a flood observation in a run's comparison",
                                  False, False, True),
    # TASK-3848: a saved setting, overwritten by the next save; not destructive.
    "set_comparison_tolerance": ("Save a project's default comparison tolerance", False, False, True),
}


def _structured_result(text: str) -> ToolResult:
    """The tool's JSON text plus the same value parsed as MCP structuredContent.

    structuredContent must be an object, so a non-object JSON value (or text that
    is not JSON) is carried as {"result": value}.
    """
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = text
    return ToolResult(
        content=text,
        structured_content=parsed if isinstance(parsed, dict) else {"result": parsed},
    )


def _tool(name: str):
    """`@mcp.tool` with this tool's annotations (TOOL_ANNOTATIONS is the one table).

    TASK-3449 (W1b, epic 3200): the REGISTERED tool is a wrapper that returns the
    JSON text plus the parsed value as structuredContent (outputSchema: object);
    the module function is returned unchanged, so direct callers still get text.
    """
    title, read_only, destructive, idempotent = TOOL_ANNOTATIONS[name]
    register = mcp.tool(
        annotations=ToolAnnotations(
            title=title, read_only_hint=read_only, destructive_hint=destructive,
            idempotent_hint=idempotent, open_world_hint=False,
        ),
        output_schema={"type": "object"},
    )

    def decorate(fn):
        @functools.wraps(fn)
        async def registered(*args, **kwargs):
            return _structured_result(await fn(*args, **kwargs))

        register(registered)
        return fn

    return decorate


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
# TASK-3187 (W0, epic 3200) — presigned-URL elision.
#
# A run record carries five `s3_*_url` keys, each a presigned S3 URL: a 6-hour
# capability (`X-Amz-Expires=21600`) carrying an STS token. A scenario detail
# carries TEN of them — `latest_run` and `latest_complete_run` are both full
# run dicts — and the read tools relayed every one of them verbatim into a
# transcript the client persists. That widens what a leaked transcript is worth
# beyond what the caller asked for, against decision D7 (epic 2467): the agent
# moves bytes only through URLs it explicitly asked for.
#
# Applied PER TOOL and NEVER inside `_post_result`: that helper is shared with
# presign_terrain_upload, whose signed `upload_url` IS what the caller asked
# for (the sole documented exception), plus finalize_terrain_upload,
# create_project, create_time_series, attach_input_layer, cancel_run and
# retry_run.
#
# Two rules, applied RECURSIVELY so a key the API adds tomorrow is covered by
# default (the six read tools' bodies are nested: list_runs is
# `{count, results: [...]}`, get_terrain composes its own envelope):
#   * KEY NAME — an `s3_*_url` key's string value is replaced outright.
#   * VALUE SHAPE — any string carrying `X-Amz-Signature` or
#     `X-Amz-Security-Token` has the signed URL REDACTED IN PLACE. In place,
#     because a run's `log` is ~17 KB of build output (clean today, but one
#     future line quoting a signed URL would otherwise silently nuke get_run's
#     whole log). The entire whitespace-delimited token goes, not just the
#     matched parameter: a presigned URL carries SIX `X-Amz-*` params, so
#     redacting only the signature would leave `X-Amz-Credential=…` behind.
# ---------------------------------------------------------------------------
ELIDED_PRESIGNED = "[presigned-url-elided]"
# Detection is the two capability-bearing params ONLY — an unsigned S3 link is
# just a link, and `bbox_stats_url` / a layer's `catalogURL` must survive.
PRESIGNED_MARKERS = ("X-Amz-Signature", "X-Amz-Security-Token")
# The whole non-whitespace run around any `X-Amz-*` param. Greedy, so one
# substitution swallows scheme, host, key and every parameter of the URL.
PRESIGNED_URL_TOKEN = re.compile(r"\S*X-Amz-\S*")


def _is_s3_url_key(key) -> bool:
    """`s3_package_url`, `s3_depth_max_url`, … — the key-name rule."""
    return isinstance(key, str) and key.startswith("s3_") and key.endswith("_url")


def _elide_presigned(value):
    """Recursively return `value` with every presigned S3 URL elided.

    Returns a new structure; the caller's body is never mutated. Non-string
    values under an `s3_*_url` key (a `null` package URL on a run that has no
    package yet) are left alone — "not there" is information the agent needs.
    """
    if isinstance(value, dict):
        return {
            key: (
                ELIDED_PRESIGNED
                if _is_s3_url_key(key) and isinstance(item, str) and item
                else _elide_presigned(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_elide_presigned(item) for item in value]
    if isinstance(value, str) and any(marker in value for marker in PRESIGNED_MARKERS):
        return PRESIGNED_URL_TOKEN.sub(ELIDED_PRESIGNED, value)
    return value


# TASK-3187 (W0, epic 3200) — the scenario fields a COMPACT read keeps, on top
# of `_build_state`'s ten flat state keys. ONE tuple shared by get_scenario and
# create_scenario so the two cannot drift: there is no `list_scenarios` tool
# among the 17 and no other read path to a scenario record, so a session that
# did not create the scenario would otherwise be blind to what it is about to
# build (`resolution`, `duration`, the input FKs, the terrain) and what it
# will cost (`compute_cost_estimate`, `vcpu_hours_estimate`) — the inverse of
# "show your work before build and spend". `inflow_anchor_mismatch` is the
# record's only pre-build correctness warning (a timeseries inflow starting
# after model_start has its first value ffilled backwards) and has no other
# MCP path at all.
SCENARIO_RECORD_KEYS = (
    "id", "project", "name", "description", "terrain", "boundary", "friction", "inflow",
    "rainfall", "building", "mesh_region", "culvert", "resolution", "duration", "computed_status",
    "mesh_triangle_count_estimate", "mesh_triangle_count_estimate_breakdown",
    "latest_run_is_valid", "compute_cost_estimate", "vcpu_hours_estimate",
    "inflow_anchor_mismatch",
)


def _scenario_record(detail) -> dict:
    """The whitelisted scenario fields PRESENT on `detail` — absent stays absent.

    Never `detail.get(key)`: STRANGER_SCENARIO_FIELDS (hydrata
    serializers_v2.py:381-400) DROPS the input FKs, both estimates,
    `inflow_anchor_mismatch` and `perms` from a non-member's read of a public
    project, and a fabricated `null` would read as "no terrain, no cost"
    instead of "withheld". A non-dict detail reads as empty, exactly as
    `_build_state` tolerates one.
    """
    if not isinstance(detail, dict):
        return {}
    return {key: detail[key] for key in SCENARIO_RECORD_KEYS if key in detail}


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
# TASK-3203 (W1, epic 3200) — the hand-back.
#
# Every tool that CHANGES something answers with three extra keys, so the
# agent can hand the engineer the link and the numbers instead of a bare "done":
#   * ui_url      — `<ui origin>/catalogue/#/map/<base_map>?panel=…&layer=…&
#                   scenario=…&run=…`; gmc's handBackParamsEpic (TASK-3205)
#                   reads the hash query, opens the panel and frames the layer.
#   * summary     — a dict of human-checkable numbers (feature count, bbox,
#                   elevation range, triangle estimate, price).
#   * say_to_user — the sentence the agent should relay.
# Composing them is BEST EFFORT and never fails the tool: the change already
# happened, so a lookup error leaves `ui_url` null and says so in
# `hand_back_error` rather than hiding the result.
# ---------------------------------------------------------------------------
HAND_BACK_PARAM_ORDER = ("panel", "layer", "scenario", "run")


def _ui_origin() -> str:
    """Where a HUMAN opens the link — never the loopback API URL (see Config.ui_origin)."""
    if config.ui_origin:
        return config.ui_origin
    if config.api_host:
        return f"https://{config.api_host}"
    return client._origin


def _ui_url(base_map, **params) -> str | None:
    """`<origin>/catalogue/#/map/<base_map>[?panel=…&layer=…&scenario=…&run=…]`, or None."""
    if base_map in (None, ""):
        return None
    query = [(key, params[key]) for key in HAND_BACK_PARAM_ORDER if params.get(key) not in (None, "")]
    url = f"{_ui_origin()}/catalogue/#/map/{base_map}"
    # ':' stays literal so `layer=geonode:bdy_1_boundary_01` reads as the alternate.
    return f"{url}?{urlencode(query, quote_via=quote, safe=':')}" if query else url


def _alternate(name) -> str | None:
    """A GeoNode layer name as its `workspace:name` alternate (gmc resolves layers by it)."""
    if not isinstance(name, str) or not name:
        return None
    return name if ":" in name else f"geonode:{name}"


async def _project_base_map(project_id) -> int | None:
    """The project's base map id (TASK-3201 put it on the V2 project record)."""
    data = await client.get(f"/projects/{project_id}/")
    return data.get("base_map") if isinstance(data, dict) else None


async def _hand_back(result: dict, project_id, summary: dict, say_to_user: str, **params) -> dict:
    """Add ui_url / summary / say_to_user to `result` (best effort, never raises)."""
    ui_url = None
    try:
        if project_id is not None:
            ui_url = _ui_url(await _project_base_map(project_id), **params)
    except Exception as exc:  # the change already landed; never hide it behind a link lookup
        result["hand_back_error"] = f"could not compose ui_url: {exc}"
    if ui_url is None:
        say_to_user = f"{say_to_user} (No map link could be composed for project {project_id}.)"
    result["ui_url"] = ui_url
    result["summary"] = summary
    result["say_to_user"] = say_to_user
    return result


async def _layer_summary(alternate) -> dict:
    """feature_count (WFS resultType=hits) + bbox_wgs84 of a published vector layer.

    Both best effort: each failure is a null plus an `*_error` string. The WFS
    goes through GeoNode's /gs/ows proxy at the API origin, so it rides the
    caller's own credential and, on prod, the loopback listener (which proxies
    only Django, never /geoserver/ directly).
    """
    summary: dict = {"layer": alternate, "feature_count": None, "bbox_wgs84": None}
    if not alternate:
        return summary
    summary["feature_count"] = await _feature_count(alternate)
    try:
        listing = await client.get_from_origin(
            "/api/v2/datasets/", params={"filter{alternate}": alternate}
        )
        datasets = listing.get("datasets") if isinstance(listing, dict) else None
        polygon = (datasets[0].get("ll_bbox_polygon") or {}) if datasets else {}
        ring = (polygon.get("coordinates") or [[]])[0]
        if ring:
            xs, ys = [pt[0] for pt in ring], [pt[1] for pt in ring]
            summary["bbox_wgs84"] = [min(xs), min(ys), max(xs), max(ys)]
    except Exception as exc:
        summary["bbox_error"] = str(exc)
    return summary


# ---------------------------------------------------------------------------
# Tool 1: list_projects
# ---------------------------------------------------------------------------
@_tool("list_projects")
async def list_projects(
    page: Annotated[int, "Page number (default 1)"] = 1,
    page_size: Annotated[int, "Results per page, max 100 (default 100)"] = 100,
) -> str:
    """List ANUGA simulation projects accessible to the authenticated user.

    Returns a paginated list of projects with their names, projections,
    and base map references.
    """
    data = await client.get("/projects/", params={"page": page, "page_size": page_size})
    return json.dumps(_elide_presigned(data), indent=2)


# ---------------------------------------------------------------------------
# Tool 2: get_project
# ---------------------------------------------------------------------------
@_tool("get_project")
async def get_project(
    project_id: Annotated[int, "The project ID"],
) -> str:
    """Get one ANUGA project: id, name, projection, visibility, your role, its base map — and the link to it.

    Returns the project record (`id`, `name`, `projection` (EPSG code),
    `simple_view_config`, `visibility`, `owner_username`, `my_role`,
    `base_map`) plus `ui_url`: the link that opens the project's map, for the
    user (`<site>/catalogue/#/map/<base_map>`; null while a new project's map
    is still being created). Scenarios are NOT listed here — use get_scenario
    with a scenario id.
    """
    data = await client.get(f"/projects/{project_id}/")
    if isinstance(data, dict):
        data = dict(data, ui_url=_ui_url(data.get("base_map")))
    return json.dumps(_elide_presigned(data), indent=2)


# ---------------------------------------------------------------------------
# Tool 3: get_scenario
# ---------------------------------------------------------------------------
@_tool("get_scenario")
async def get_scenario(
    project_id: Annotated[int, "The project ID"],
    scenario_id: Annotated[int, "The scenario ID"],
) -> str:
    """Get a scenario's COMPACT state: its `computed_status`, inputs, estimate and price.

    Returns a compact record, never the raw ~50 KB detail: `scenario_id`,
    `computed_status`, `mesh_triangle_count_estimate` and its `_breakdown`,
    `latest_run_is_valid`, the latest run's `run_id`, `run_status`,
    `error_message`, `user_message`, `status_detail` and `mesh_triangle_count`
    — plus what the scenario IS and what it will cost: `name`, `description`,
    `project`, `resolution`, `duration`, the input row ids (`terrain`,
    `boundary`, `friction`, `inflow`, `rainfall`, `building`, `mesh_region`, `culvert`),
    `compute_cost_estimate` (USD), `vcpu_hours_estimate`, and
    `inflow_anchor_mismatch` (null unless an inflow series starts after the
    model does, whose first value is then held backwards).

    The `computed_status` field (there is NO `status` key on a scenario) is
    derived from the latest run and will be one of: created, building, built,
    queued, computing, processing, complete, error, or cancelled — `created`
    also means no run exists yet. A scenario must be `built` before it can be
    run. `latest_run_is_valid` is false after any edit since the last build.
    A non-member reading a public project's scenario gets a reduced record:
    the fields the server withholds (the input ids, the price,
    `inflow_anchor_mismatch`) are ABSENT here rather than reported as null —
    "withheld" is not "no terrain, no cost" — while the run-state keys,
    `computed_status` among them, read null.

    No presigned S3 URL is returned by any read tool (each is a 6-hour
    capability): results stay viewable through a run's `gn_layer_*` WMS
    entries — use get_run for the full run record.
    """
    detail = await client.get(f"/projects/{project_id}/scenarios/{scenario_id}/")
    # TASK-3187 (W0, epic 3200) — the compact state, not the 52 KB detail: the
    # two run blocks carry TEN presigned S3 URLs and a ~17 KB build log, and
    # get_run is the tool for a run. `_build_state` UNFORKED + the shared
    # scenario whitelist through its `**extra` kwarg. `_build_state` elides its
    # whole return (including this `**extra`, which copies free text such as
    # `description` that the caller does not control), so there is no second
    # elision call here — W0 simplify pass.
    return json.dumps(_build_state(detail, **_scenario_record(detail)), indent=2)


# ---------------------------------------------------------------------------
# Tool 4: start_simulation
# ---------------------------------------------------------------------------
@_tool("start_simulation")
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
    if the scenario is not in the correct state. The returned run record has
    its presigned `s3_*_url` links elided (6-hour capabilities).
    """
    body, status_code = await client.post(
        f"/scenarios/{scenario_id}/run/",
        json={"compute_backend": compute_backend},
    )
    # TASK-3187 — the 202 relays RunSerializerV2 for a run that is already
    # `built`, so it ALREADY holds the signed package URL: elide here, never in
    # `_post_result` (presign_terrain_upload shares it and must keep its URL).
    result = json.loads(_post_result(_elide_presigned(body), status_code))
    run_id = result.get("id")
    await _hand_back(
        result, result.get("project"),
        {"run_id": run_id, "scenario_id": scenario_id, "status": result.get("status"),
         "compute_backend": result.get("compute_backend", compute_backend),
         "vcpu_hours_estimate": result.get("vcpu_hours_estimate")},
        f"Run {run_id} has started for scenario {scenario_id} ({result.get('status')}). "
        "Open the link to watch it; the results appear on the map when it completes.",
        run=run_id,
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 5: get_run_status
# ---------------------------------------------------------------------------
@_tool("get_run_status")
async def get_run_status(
    run_id: Annotated[int, "The run ID"],
) -> str:
    """Lightweight status check for a simulation run (fast, <50ms).

    Use this for polling instead of get_run. Returns only: id, status,
    `phase`, progress_pct (0-100), eta_seconds, error_message, and
    compute_backend.

    `phase` is never empty: it is `status`, except `preparing_inputs` while a
    `computing` run has no progress yet (it is downloading its input package
    and building the domain — minutes on a big model; this is NOT a stall).
    Poll every 5-10 seconds. Terminal states: complete, error, cancelled.
    """
    data = await client.get(f"/runs/{run_id}/status/")
    if isinstance(data, dict):
        data = dict(data, phase=_run_phase(data))
    return json.dumps(_elide_presigned(data), indent=2)


def _run_phase(status_record: dict) -> str:
    """TASK-3204 — the 09-21 drive read a blank progress poll as a stall. Mirrors
    gmc TASK-2392's 'Preparing inputs…': a computing run with no progress yet is
    still downloading its package. Never empty ('unknown' if the API sends none)."""
    status = status_record.get("status") or "unknown"
    if status == "computing" and status_record.get("progress_pct") is None:
        return "preparing_inputs"
    return status


# ---------------------------------------------------------------------------
# Tool 6: get_run
# ---------------------------------------------------------------------------
@_tool("get_run")
async def get_run(
    run_id: Annotated[int, "The run ID"],
) -> str:
    """Get full details of a simulation run including timing and results.

    Returns the complete run record: status, progress, timing (start/end
    timestamps, duration), compute details (backend, instance type, cost),
    mesh info, error messages, and result log. Use get_run_status for
    lightweight polling; use this for final results.

    The presigned `s3_*_url` download links are ELIDED — each is a 6-hour
    capability and no tool relays one. The results stay viewable through the
    `gn_layer_*` entries (published WMS layers on the project's map).
    """
    data = await client.get(f"/runs/{run_id}/")
    return json.dumps(_elide_presigned(data), indent=2)


# ---------------------------------------------------------------------------
# Tool 7: cancel_run
# ---------------------------------------------------------------------------
@_tool("cancel_run")
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
    result = json.loads(_post_result(body, status_code))
    project_id, scenario_id = await _run_project_and_scenario(run_id, result)
    await _hand_back(
        result, project_id,
        {"run_id": run_id, "scenario_id": scenario_id, "status": result.get("status")},
        f"Run {run_id} is cancelled ({result.get('status')}); its compute has been released. "
        "Open the link to see the scenario.",
        panel="scenarios", scenario=scenario_id,
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 8: retry_run
# ---------------------------------------------------------------------------
@_tool("retry_run")
async def retry_run(
    run_id: Annotated[int, "The run ID to retry"],
) -> str:
    """Retry a failed simulation run.

    Resets an errored run back to 'created' status and triggers a new
    package build. The same run ID is reused. Only valid when
    status is 'error'. Returns 409 for any other state.
    """
    body, status_code = await client.post(f"/runs/{run_id}/retry/")
    result = json.loads(_post_result(body, status_code))
    project_id, scenario_id = await _run_project_and_scenario(run_id, result)
    await _hand_back(
        result, project_id,
        {"run_id": run_id, "scenario_id": scenario_id, "status": result.get("status"),
         "rebuilding": result.get("rebuilding")},
        f"Run {run_id} is being retried: scenario {scenario_id} is rebuilding as a NEW run. "
        "Open the link to follow the build.",
        panel="scenarios", scenario=scenario_id,
    )
    return json.dumps(result, indent=2)


async def _run_project_and_scenario(run_id, body) -> tuple:
    """(project, scenario) of a run: from `body` when it carries them, else one GET.

    cancel/retry answer a short body; the run record has both ids. Best effort:
    (None, None) on failure, and the hand-back then says no link was composed.
    """
    if isinstance(body, dict) and body.get("project") is not None:
        return body.get("project"), body.get("scenario")
    try:
        run = await client.get(f"/runs/{run_id}/")
    except Exception:
        return None, None
    return (run.get("project"), run.get("scenario")) if isinstance(run, dict) else (None, None)


# ---------------------------------------------------------------------------
# Tool 9: list_runs
# ---------------------------------------------------------------------------
@_tool("list_runs")
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
    to find active, completed, or failed runs. Each row's presigned
    `s3_*_url` download links are elided (6-hour capabilities).
    """
    params: dict = {"page": page, "page_size": page_size}
    if status_filter:
        params["status"] = status_filter
    data = await client.get(f"/projects/{project_id}/runs/", params=params)
    return json.dumps(_elide_presigned(data), indent=2)


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
# Building/MeshRegion rows that W1.2's attach_input_layer PATCHes.
# ---------------------------------------------------------------------------

# Terrain.status runs creating → styling → ready | error (gn_anuga tasks.py
# :1473/:1542/:1620); only the two terminal states end a poll.
TERRAIN_TERMINAL_STATUSES = frozenset({"ready", "error"})


# ---------------------------------------------------------------------------
# Tool 10: create_project
# ---------------------------------------------------------------------------
@_tool("create_project")
async def create_project(
    name: Annotated[str, "Project name"],
    projection: Annotated[
        str,
        "Working CRS as an EPSG string, normally the UTM zone of the site, "
        "e.g. 'EPSG:32756' (WGS 84 / UTM 56S)",
    ],
) -> str:
    """Create a new ANUGA project. Returns the project record including its id, base_map and ui_url.

    The caller's own account becomes the owner. `projection` is the projected
    CRS every scenario in the project is meshed and run in — pick the UTM zone
    covering the site. The project's map is created alongside it; `ui_url`
    opens it — give it to the user. Next step for a new project:
    presign_terrain_upload.
    """
    body, status_code = await client.post(
        "/projects/", json={"name": name, "projection": projection}
    )
    result = json.loads(_post_result(body, status_code))
    project_id = result.get("id")
    if project_id is not None and result.get("base_map") is None:
        # The map can be created a moment after the 201: re-read the detail once.
        try:
            detail = await client.get(f"/projects/{project_id}/")
            if isinstance(detail, dict):
                result["base_map"] = detail.get("base_map")
        except HydrataAPIError:
            pass
    await _hand_back(
        result, project_id,
        {"project_id": project_id, "name": result.get("name", name),
         "projection": result.get("projection", projection), "base_map": result.get("base_map")},
        f"Project '{result.get('name', name)}' (id {project_id}, {projection}) is created. "
        "Here is its map; next I will upload the terrain.",
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 11: presign_terrain_upload
# ---------------------------------------------------------------------------
@_tool("presign_terrain_upload")
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
@_tool("finalize_terrain_upload")
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
    boundary, friction, inflow, rainfall, building and mesh-region rows.
    Returns 202 with the terrain record; keep its `id` for get_terrain. A 400 UPLOAD_NOT_FOUND
    means no object is at `staging_key`: the PUT did not land (check its status
    code and Content-Type) — do not retry finalize until it has.
    """
    body, status_code = await client.post(
        f"/projects/{project_id}/terrain/upload/finalize/",
        json={"staging_key": staging_key, "process_id": process_id, "title": title},
    )
    result = json.loads(_post_result(body, status_code))
    await _hand_back(
        result, project_id,
        {"terrain_id": result.get("id"), "title": result.get("title") or title,
         "status": result.get("status")},
        f"The terrain upload is registered (terrain {result.get('id')}, {result.get('status')}). "
        "The import takes minutes; I will tell you when it is ready to check on the map.",
        panel="inputs",
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 13: get_terrain
# ---------------------------------------------------------------------------
@_tool("get_terrain")
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
    instead) — plus the last `status` seen (creating → styling → ready | error)
    and `phase` (that status, or `not_found`; never empty),
    `polls`, `elapsed_seconds` and the full `terrain` record (its `gn_layer` is
    the published elevation dataset pk once ready). An import takes minutes to
    tens of minutes (the SAME 32 MB 1 m DEM measured 4.5 min once and 26 min
    once — the worker's S3 download speed dominates), so `timed_out` is normal
    and NOT a failure: keep calling with the same arguments while `status` is
    still `creating`/`styling`; several calls in a row is expected. `error` is
    terminal — the import failed and no default input rows were seeded; upload
    a corrected GeoTIFF as a new terrain.

    `ready` is written by the import task; the project's default input rows
    (Boundary 01, Friction 01, Inflow 01, Rainfall 01, Building 01,
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
        # TASK-3204 — never empty: the import stage, or `not_found`.
        "phase": status or ("not_found" if terrain is None else "unknown"),
        "polls": polls,
        "elapsed_seconds": elapsed,
        "terrain": terrain,
    }
    if terrain_id is None:
        result["terrain_count"] = terrain_count
    record = terrain if isinstance(terrain, dict) else {}
    summary = {key: record.get(key) for key in (
        "id", "bbox_wgs84", "dem_elev_min", "dem_elev_max", "nodata_fraction",
        "native_crs", "native_resolution_m",
    )}
    if outcome == "ready":
        nodata = record.get("nodata_fraction")
        nodata = f"{nodata:.1%}" if isinstance(nodata, (int, float)) else "an unknown share"
        say = (
            f"The terrain is ready: elevation {record.get('dem_elev_min')} to "
            f"{record.get('dem_elev_max')} m, {nodata} of cells without data, native CRS "
            f"{record.get('native_crs')}. Open the link and check it covers the whole site."
        )
    elif outcome == "error":
        say = "The terrain import failed; the GeoTIFF needs fixing and uploading again."
    elif outcome == "not_found":
        say = "This project has no terrain yet."
    else:
        say = f"The terrain is still importing ({status}); I am still waiting on it."
    await _hand_back(
        result, project_id, summary, say,
        panel="inputs", layer=_alternate(record.get("gn_layer_name")) if outcome == "ready" else None,
    )
    # TASK-3187 — this tool composes its own envelope, so elide the COMPOSED
    # result; the terrain record carries no signed key today (future-proofing).
    return json.dumps(_elide_presigned(result), indent=2)


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
# and a POST to /buildings/ or /mesh-regions/ carrying gn_layer is silently
# OVERWRITTEN: perform_create (api_v2.py :5247-5261) unconditionally queues
# create_layer_for_model, which makes an empty dataset and re-saves gn_layer
# moments after the 201.
INPUT_LAYER_ROUTES = {
    "boundary": "boundaries",
    "friction": "frictions",
    "inflow": "inflows",
    "rainfall": "rainfalls",
    "building": "buildings",
    "mesh_region": "mesh-regions",
    # TASK-3641 (epic 3585): Hydraulic Structures. Unlike the six above, the
    # terrain chain seeds NO culvert row, so attach_input_layer creates the
    # default 'Culvert 01' on demand (POST, then waits for the layer factory
    # to finish BEFORE its PATCH, so the factory cannot overwrite gn_layer).
    "culvert": "culverts",
}
# TASK-3586: the building-footprint layer was called 'structure' before the
# Structure -> Building rename. The tool is an external contract, so the old
# kind keeps working for one release and answers with a deprecation note.
LEGACY_INPUT_LAYER_KINDS = {"structure": "building"}
LEGACY_KIND_DEPRECATION = (
    "kind 'structure' is deprecated and will be removed next release: the "
    "building-footprint layer is kind 'building' (Structures now means hydraulic "
    "structures such as culverts)."
)
# The title create_supporting_models gives each default row; used to pick the
# row deterministically when a project has more than one (the list has no
# declared ordering).
INPUT_LAYER_DEFAULT_TITLES = {
    "boundary": "Boundary 01",
    "friction": "Friction 01",
    "inflow": "Inflow 01",
    "rainfall": "Rainfall 01",
    "building": "Building 01",
    "mesh_region": "MeshRegion 01",
    "culvert": "Culvert 01",
}
# TASK-3586: a pre-rename project's default building row is titled 'Structure 01'.
LEGACY_DEFAULT_TITLES = {"building": ("Structure 01",)}
# No REST create path exists for breaklines at HEAD (api_v2.py:3392: "no
# create path exists today (TASK-3040 AC6)"), so the tool refuses up front
# rather than pretending the layer landed. Culverts left this set in TASK-3641
# (epic 3585 W2b): the culvert route, Scenario.culvert and the package key exist.
UNSUPPORTED_INPUT_LAYER_KINDS = frozenset({"breakline"})
UNSUPPORTED_INPUT_LAYER_REASON = (
    "no REST create path exists for breaklines at HEAD (TASK-3040 AC6 "
    "retired the UI surface; the model + Scenario FK stay dormant) — attaching "
    "one would only pretend the layer landed"
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
@_tool("create_time_series")
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
    rows = data["rowData"]
    if isinstance(landed, dict) and isinstance(landed.get("rowData"), list):
        rows = landed["rowData"]
        row_count = len(rows)
    result = {
        "id": body.get("id"),
        "name": body.get("name", name),
        "series_type": body.get("series_type", series_type),
        "units": body.get("units", units),
        "timezone": body.get("timezone", timezone),
        "row_count": row_count,
        "http_status": status_code,
    }
    first = rows[0].get("timestamp") if rows and isinstance(rows[0], dict) else None
    last = rows[-1].get("timestamp") if rows and isinstance(rows[-1], dict) else None
    await _hand_back(
        result, project_id,
        {"series_id": result["id"], "name": result["name"], "series_type": result["series_type"],
         "units": result["units"], "row_count": row_count, "first_timestamp": first,
         "last_timestamp": last},
        f"Time series '{result['name']}' ({result['series_type']}, {row_count} rows, "
        f"{first} to {last}, {result['units'] or 'no units'}) is saved. Open the link to "
        "check its shape in the Hydrology panel.",
        panel="hydrology",
    )
    return json.dumps(result, indent=2)


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
    titles = (INPUT_LAYER_DEFAULT_TITLES[kind], *LEGACY_DEFAULT_TITLES.get(kind, ()))
    for title in titles:
        for row in rows:
            if isinstance(row, dict) and row.get("title") == title:
                return row
    return min(rows, key=lambda row: row.get("id", 0) if isinstance(row, dict) else 0)


# ---------------------------------------------------------------------------
# TASK-3641 (W2b, epic 3585) — culvert attribute mapping.
#
# The build reads each culvert's details from the layer's columns BY THEIR D10
# NAMES (hydrata gn_anuga/models/culvert.py CULVERT_FIELDS) and refuses a
# culvert missing any it needs; the true-scale style draws barrels from them.
# An agent's GeoJSON rarely uses those names (the Towradgi bundle says
# `shape: pipe`, `anuga_label`, `barrel_manning`), and attach_input_layer
# never sees the file. So for kind=culvert the tool reads the uploaded
# features back over WFS, renames recognised attributes onto the D10 fields,
# fills the D10 defaults (the map's WFS-T inserts send explicit NULLs, so the
# layer must carry them, not a column DEFAULT), keeps every other attribute
# verbatim, and re-uploads that as the culvert layer. A layer that already
# reads as D10 with defaults filled is attached as uploaded.
# ---------------------------------------------------------------------------
CULVERT_FIELDS = (
    "name", "shape", "width_m", "height_m", "diameter_m",
    "upstream_invert_m", "downstream_invert_m", "barrels", "wall_thickness_m",
    "entrance_type", "material", "manning_n", "blockage_pct",
    "length_override_m", "description",
)
CULVERT_NUMBER_FIELDS = (
    "width_m", "height_m", "diameter_m", "upstream_invert_m", "downstream_invert_m",
    "wall_thickness_m", "manning_n", "blockage_pct", "length_override_m",
)
# Mirrors CULVERT_FEATURE_DEFAULTS + MATERIAL_MANNING_N (hydrata culvert.py).
CULVERT_DEFAULTS = {
    "barrels": 1,
    "wall_thickness_m": 0.20,
    "entrance_type": "square_edge_headwall",
    "material": "concrete",
    "blockage_pct": 0.0,
}
CULVERT_MATERIAL_MANNING_N = {"concrete": 0.013, "corrugated_metal": 0.024}
# TASK-3662: the 14 valid (entrance code, shape, material) combinations. Mirrors
# hydrata gn_anuga.models.culvert.CULVERT_ENTRANCE_TYPES (pinned equal to the gmc
# ENTRANCE_TYPES table and the engine catalogue); the build refuses any other.
CULVERT_ENTRANCE_TYPES = (
    ("square_edge_headwall", "circular", "concrete"),
    ("groove_end_headwall", "circular", "concrete"),
    ("groove_end_projecting", "circular", "concrete"),
    ("bevelled_ring_45", "circular", "concrete"),
    ("square_edge_headwall", "circular", "corrugated_metal"),
    ("mitred_to_slope", "circular", "corrugated_metal"),
    ("projecting", "circular", "corrugated_metal"),
    ("square_edge_headwall", "box", "concrete"),
    ("wingwalls_30_75", "box", "concrete"),
    ("wingwalls_0", "box", "concrete"),
    ("wingwalls_45_top_bevel", "box", "concrete"),
    ("headwall_bevelled_45", "box", "concrete"),
    ("square_edge_headwall", "box", "corrugated_metal"),
    ("thin_wall_projecting", "box", "corrugated_metal"),
)
CULVERT_ENTRANCE_CODES = frozenset(code for code, _shape, _material in CULVERT_ENTRANCE_TYPES)
CULVERT_MAX_BARRELS = 20
# Other attribute names (matched case-insensitively) taken for each D10 field,
# in priority order. The D10 name itself always wins when it carries a value.
CULVERT_ATTRIBUTE_ALIASES = {
    "name": ("label", "anuga_label", "culvert_name", "structure_name"),
    "shape": ("culvert_shape", "section", "section_shape", "cross_section"),
    "width_m": ("width", "span", "span_m", "box_width"),
    "height_m": ("height", "rise", "rise_m", "box_height"),
    "diameter_m": ("diameter", "dia", "diam", "pipe_diameter"),
    "upstream_invert_m": ("upstream_invert", "us_invert", "us_invert_m", "inlet_invert",
                          "inlet_invert_m", "invert_us", "invert_upstream"),
    "downstream_invert_m": ("downstream_invert", "ds_invert", "ds_invert_m", "outlet_invert",
                            "outlet_invert_m", "invert_ds", "invert_downstream"),
    "barrels": ("barrel_count", "num_barrels", "n_barrels", "number_of_barrels", "no_barrels", "cells"),
    "wall_thickness_m": ("wall_thickness", "wall_m"),
    "entrance_type": ("entrance", "inlet_type", "entrance_loss_type"),
    "material": ("culvert_material", "barrel_material"),
    "manning_n": ("manning", "mannings_n", "manning_s_n", "barrel_manning", "roughness"),
    "blockage_pct": ("blockage", "blockage_percent", "blocked_pct", "blockage_percentage"),
    "length_override_m": ("length", "length_m", "barrel_length", "culvert_length"),
    "description": ("desc", "notes", "comment", "comments"),
}
CULVERT_SHAPE_ALIASES = {
    "box": "box", "rectangular": "box", "rectangle": "box", "rect": "box", "rcbc": "box",
    "circular": "circular", "circle": "circular", "pipe": "circular", "round": "circular",
    "circ": "circular", "rcp": "circular",
}
CULVERT_MATERIAL_ALIASES = {
    "concrete": "concrete", "rc": "concrete", "rcp": "concrete", "rcbc": "concrete",
    "reinforced_concrete": "concrete",
    "corrugated_metal": "corrugated_metal", "cmp": "corrugated_metal",
    "corrugated_steel": "corrugated_metal", "corrugated_metal_pipe": "corrugated_metal",
}
# The importer's primary-key column; never a culvert attribute.
CULVERT_SKIPPED_ATTRIBUTES = frozenset({"fid", "ogc_fid"})
# The most culverts one attach reads back (WFS maxFeatures is this + 1).
CULVERT_MAX_FEATURES = 2000
CULVERT_UPLOAD_PATH = "/api/v2/uploads/upload/"


def _culvert_number(value):
    """A finite float for a numeric-looking value; anything else unchanged."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return number if math.isfinite(number) else value


def _culvert_slug(value):
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _map_culvert_properties(properties: dict) -> tuple[dict, dict, list]:
    """One feature's attributes renamed onto the D10 fields, with defaults filled.

    Returns (properties, renamed {source attribute: D10 field}, filled [D10
    fields given a default]). Unrecognised attributes are kept verbatim; an
    alias whose D10 field already had a value is dropped (the D10 name wins).
    """
    source = {key: value for key, value in (properties or {}).items()
              if key.lower() not in CULVERT_SKIPPED_ATTRIBUTES}
    by_lower = {key.strip().lower(): key for key in source}
    out: dict = {}
    renamed: dict = {}
    consumed = set()
    for field in CULVERT_FIELDS:
        value = None
        for candidate in (field, *CULVERT_ATTRIBUTE_ALIASES[field]):
            key = by_lower.get(candidate)
            if key is None:
                continue
            consumed.add(key)
            if (value is None or value == "") and source[key] not in (None, ""):
                value = source[key]
                if key != field:
                    renamed[key] = field
        out[field] = value
    for field in CULVERT_NUMBER_FIELDS:
        out[field] = _culvert_number(out[field])
    barrels = _culvert_number(out["barrels"])
    if isinstance(barrels, float) and barrels.is_integer():
        barrels = int(barrels)
    out["barrels"] = barrels
    if isinstance(out["shape"], str) and out["shape"].strip():
        out["shape"] = CULVERT_SHAPE_ALIASES.get(_culvert_slug(out["shape"]), out["shape"])
    if isinstance(out["material"], str) and out["material"].strip():
        out["material"] = CULVERT_MATERIAL_ALIASES.get(_culvert_slug(out["material"]), out["material"])
    if isinstance(out["entrance_type"], str) and _culvert_slug(out["entrance_type"]) in CULVERT_ENTRANCE_CODES:
        out["entrance_type"] = _culvert_slug(out["entrance_type"])
    filled = []
    for field, default in CULVERT_DEFAULTS.items():
        if out[field] is None or out[field] == "":
            out[field] = default
            filled.append(field)
    if out["manning_n"] is None and out["material"] in CULVERT_MATERIAL_MANNING_N:
        out["manning_n"] = CULVERT_MATERIAL_MANNING_N[out["material"]]
        filled.append("manning_n")
    extras = {key: value for key, value in source.items() if key not in consumed}
    return {**out, **extras}, renamed, filled


def _culvert_vertex_count(geometry) -> int | None:
    """Vertices of a LineString (or a single-part MultiLineString, as the importer stores it)."""
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    if geometry.get("type") == "MultiLineString" and isinstance(coordinates, list) and len(coordinates) == 1:
        coordinates = coordinates[0]
    elif geometry.get("type") != "LineString":
        return None
    return len(coordinates) if isinstance(coordinates, list) else None


def _culvert_problems(index: int, properties: dict, geometry) -> list:
    """What the build will refuse about one mapped culvert (advisory; the build decides)."""
    label = f"Culvert '{properties['name']}'" if properties.get("name") else f"Culvert #{index + 1}"
    problems = []
    if _culvert_vertex_count(geometry) != 2:
        problems.append("must be a straight line of exactly two points (Inlet, then Outlet)")
    shape = properties.get("shape")
    if shape not in ("box", "circular"):
        problems.append(f"shape must be 'box' or 'circular' (got {shape!r})")
    else:
        sizes = ("width_m", "height_m") if shape == "box" else ("diameter_m",)
        for key in sizes:
            value = properties.get(key)
            if not isinstance(value, float) or value <= 0:
                problems.append(f"{key} must be a positive number for a {shape} culvert")
    material = properties.get("material")
    if material not in CULVERT_MATERIAL_MANNING_N:
        problems.append("material must be 'concrete' or 'corrugated_metal'")
    elif shape in ("box", "circular"):
        valid = [code for code, s, m in CULVERT_ENTRANCE_TYPES if s == shape and m == material]
        entrance = properties.get("entrance_type")
        if entrance not in valid:
            problems.append(
                f"entrance_type {entrance!r} is not available for a {shape} "
                f"{material.replace('_', ' ')} culvert (choose one of: {', '.join(valid)})")
    for key in ("upstream_invert_m", "downstream_invert_m"):
        if not isinstance(properties.get(key), float):
            problems.append(f"{key} is required")
    barrels = properties.get("barrels")
    if not isinstance(barrels, int) or isinstance(barrels, bool) or not 1 <= barrels <= CULVERT_MAX_BARRELS:
        problems.append(f"barrels must be a whole number from 1 to {CULVERT_MAX_BARRELS}")
    blockage = properties.get("blockage_pct")
    if not isinstance(blockage, float) or not 0 <= blockage < 100:
        problems.append("blockage_pct must be from 0 to below 100")
    return [f"{label}: " + "; ".join(problems)] if problems else []


def _comparable(properties: dict) -> dict:
    """Attributes as the rewrite check compares them: no nulls (a WFS read may omit
    or include a null column alike) and no importer key (GeoServer's JSON carries
    the `fid` primary key as a property)."""
    return {key: value for key, value in properties.items()
            if value is not None and key.lower() not in CULVERT_SKIPPED_ATTRIBUTES}


def _map_culvert_collection(collection) -> tuple[dict, dict]:
    """The uploaded culverts mapped onto D10, plus a report.

    report = {feature_count, renamed (source -> D10, union), filled (D10 field ->
    how many culverts took the default), problems [...], rewrite (True when any
    feature changed, i.e. the upload is not already a D10 culvert layer)}.
    """
    features = (collection or {}).get("features") or []
    mapped, renamed, filled, problems, rewrite = [], {}, {}, [], False
    for index, feature in enumerate(features):
        original = (feature or {}).get("properties") or {}
        properties, feature_renamed, feature_filled = _map_culvert_properties(original)
        renamed.update(feature_renamed)
        for field in feature_filled:
            filled[field] = filled.get(field, 0) + 1
        problems.extend(_culvert_problems(index, properties, feature.get("geometry")))
        rewrite = rewrite or _comparable(properties) != _comparable(original)
        mapped.append({"type": "Feature", "geometry": feature.get("geometry"), "properties": properties})
    out = {"type": "FeatureCollection", "features": mapped}
    if (collection or {}).get("crs"):
        out["crs"] = collection["crs"]
    report = {"feature_count": len(mapped), "renamed": renamed, "filled": filled,
              "problems": problems, "rewrite": rewrite}
    return out, report


async def _read_culvert_features(alternate) -> dict:
    """The uploaded layer's features as GeoJSON, in its native CRS (WFS 1.0.0: x/y order)."""
    try:
        collection = await client.get_from_origin("/gs/ows", params={
            "service": "WFS", "version": "1.0.0", "request": "GetFeature",
            "typeName": alternate, "outputFormat": "application/json",
            "maxFeatures": CULVERT_MAX_FEATURES + 1,
        })
    except (HydrataAPIError, ValueError) as exc:
        raise ToolError(
            f"could not read the uploaded culverts back from {alternate} over WFS ({exc}); "
            "nothing was attached — call again with the same arguments"
        )
    if not isinstance(collection, dict) or not isinstance(collection.get("features"), list):
        raise ToolError(f"the WFS read of {alternate} returned no feature collection; nothing was attached")
    if len(collection["features"]) > CULVERT_MAX_FEATURES:
        raise ToolError(
            f"{alternate} holds more than {CULVERT_MAX_FEATURES} culverts; split the file — "
            "nothing was attached"
        )
    return collection


# ---------------------------------------------------------------------------
# Tool 15: attach_input_layer
# ---------------------------------------------------------------------------
@_tool("attach_input_layer")
async def attach_input_layer(
    project_id: Annotated[int, "The project ID"],
    kind: Annotated[
        str,
        "Which input the uploaded layer is: boundary, friction, inflow, rainfall, "
        "building, mesh_region or culvert ('structure' is a deprecated alias of "
        "building). breakline is refused (see below).",
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
    """Attach a GeoJSON you uploaded to GeoNode as the project's boundary, friction, inflow, rainfall, building, mesh_region or culvert layer.

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

    kind `culvert` (Hydraulic Structures; one two-point line per culvert,
    Inlet first, then Outlet): no culvert row is seeded, so the tool creates
    'Culvert 01' when the project has none and waits for its layer. It reads
    your culverts back and maps your attribute names onto the culvert fields
    — name, shape (box|circular), width_m, height_m, diameter_m,
    upstream_invert_m, downstream_invert_m, barrels (1-20), wall_thickness_m,
    entrance_type, material (concrete|corrugated_metal), manning_n,
    blockage_pct (Reduced-area blockage, percent), length_override_m,
    description — accepting common alternatives (width, diameter, label,
    us_invert, ds_invert, num_barrels, manning, blockage, length, shape
    'pipe' = circular, …) and filling the defaults (barrels 1, wall 0.20 m,
    square_edge_headwall, concrete, manning_n from the material, blockage
    0). Other attributes are kept. If anything changed it re-uploads the
    mapped culverts and attaches THAT layer (`mapped`: renamed, filled,
    source_dataset_pk). `culvert_problems` lists what the build will still
    refuse (typically missing inverts) — the user fixes those in the map.
    `leftover_datasets` lists the datasets the attach left unreferenced (the
    raw upload when it was re-uploaded; the empty factory layer of a
    'Culvert 01' created here), each {dataset_pk, name, reason}, with a
    `leftover_hint`; the tool deletes nothing — the user may delete them.
    The culvert engine conveys flow through each culvert in a run (GPU
    lanes and localhost; the multi-core CPU lanes refuse a culvert scenario).

    `outcome` is exactly one of: `attached` (row_id, gn_layer, dataset_pk,
    dataset_alternate — the WFS typename — and every row id seen);
    `timed_out` (still importing or still creating the culvert layer: call
    again with the same arguments, except pass `retry_execution_id` as
    execution_id when the result carries one); `upload_failed` (the
    execution record, incl. its log, is returned verbatim — fix the file and
    upload again); `no_default_row`; `no_dataset` (finished but no resource
    was recorded). The PATCH is refused with a 400 if the dataset is not
    owned by you.

    Refused, with no request made: kind `breakline` — no REST create path
    exists for it at HEAD (TASK-3040 AC6), so attaching one would only
    pretend. Also refused without a request: an execution_id that is not the
    UUID the upload returned.
    One GeoJSON per kind: all of a kind's features travel in that one file. A
    rainfall polygon names its gauge in its `data` property — create that
    gauge with create_time_series under the exact same name.
    """
    started = time.monotonic()
    kind = kind.strip().lower().replace("-", "_")
    deprecation = None
    if kind in LEGACY_INPUT_LAYER_KINDS:
        kind, deprecation = LEGACY_INPUT_LAYER_KINDS[kind], LEGACY_KIND_DEPRECATION
    if kind in UNSUPPORTED_INPUT_LAYER_KINDS:
        # TASK-3203 (RE-REVIEW AC2) — the link, but NOT "draw it in the map": the
        # map has no breakline tool either (TASK-3040 AC6 retired it).
        # One read-only GET for the base map; no write is attempted.
        try:
            link = _ui_url(await _project_base_map(project_id), panel="inputs")
        except Exception:
            link = None
        raise ToolError(
            f"kind {kind!r} is refused: {UNSUPPORTED_INPUT_LAYER_REASON}. "
            f"Accepted kinds: {', '.join(INPUT_LAYER_ROUTES)}. "
            f"say_to_user: {kind} layers are not supported by Hydrata yet — neither this "
            f"tool nor the map can add one. ui_url: {link}"
        )
    if kind not in INPUT_LAYER_ROUTES:
        raise ToolError(
            f"kind {kind!r} is not one of {', '.join(INPUT_LAYER_ROUTES)} "
            "(breakline is refused: no REST create path at HEAD, TASK-3040 AC6)"
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
    label = kind.replace("_", " ")

    # ONE budget for every wait of this call (the culvert path polls up to three
    # times), clamped like _poll_until so the sum stays under prod's proxy cut.
    budget = max(0, min(int(timeout_seconds), POLL_MAX_TIMEOUT_SECONDS))

    def remaining() -> float:
        return max(0.0, budget - (time.monotonic() - started))

    async def handed_back(result, summary, say, **params):
        if deprecation:
            result["deprecation"] = deprecation
        await _hand_back(result, project_id, dict({"kind": kind}, **summary), say,
                         panel="inputs", **params)
        return json.dumps(result, indent=2)

    upload = await _await_upload(execution_id, budget, poll_interval_seconds)
    status = upload["execution_status"]
    if upload["timed_out"]:
        return await handed_back(
            {
                "outcome": "timed_out",
                "kind": kind,
                "execution_status": status,
                "polls": upload["polls"],
                "elapsed_seconds": upload["elapsed_seconds"],
            },
            {"execution_status": status},
            f"The {label} upload is still importing; I am still waiting on it.",
        )

    result: dict = {
        "outcome": None,
        "kind": kind,
        "route": route,
        "execution_status": status,
        "polls": upload["polls"],
        "elapsed_seconds": upload["elapsed_seconds"],
    }
    if status == "failed":
        result["outcome"] = "upload_failed"
        result["execution"] = upload["execution"]  # verbatim: the log names the import error
        return await handed_back(
            result, {"execution_status": status},
            f"The {label} file failed to import; it needs fixing before it can be attached.",
        )

    dataset_pk = upload["dataset_pk"]
    result["dataset_pk"] = dataset_pk
    if dataset_pk is None:
        result["outcome"] = "no_dataset"
        result["execution"] = upload["execution"]
        return await handed_back(
            result, {"execution_status": status},
            f"The {label} upload finished but produced no layer, so nothing was attached.",
        )

    culvert_report = None
    # TASK-3660 (operator AC0 = REPORT): datasets this attach leaves
    # unreferenced. Reported only, never deleted (accountable agents, epic 3200).
    leftovers: list = []
    if kind == "culvert":
        # TASK-3641 — read back, map onto D10, re-upload when anything changed.
        source_alternate = await _dataset_alternate(dataset_pk)
        if not source_alternate:
            raise ToolError(f"dataset {dataset_pk} has no layer name to read the culverts from; nothing was attached")
        collection, culvert_report = _map_culvert_collection(await _read_culvert_features(source_alternate))
        rewrite = culvert_report.pop("rewrite")
        result["culvert_problems"] = culvert_report.pop("problems")
        result["mapped"] = dict(culvert_report, source_dataset_pk=dataset_pk, reuploaded=rewrite)
        if rewrite:
            body, _status_code = await client.post_file_to_origin(
                CULVERT_UPLOAD_PATH, "base_file", f"cul_{project_id}_culverts.geojson",
                json.dumps(collection).encode("utf-8"), "application/geo+json",
            )
            mapped_execution_id = body.get("execution_id") if isinstance(body, dict) else None
            if not mapped_execution_id:
                raise ToolError(
                    f"the mapped culvert re-upload returned no execution_id ({body!r}); nothing was attached"
                )
            result["retry_execution_id"] = str(mapped_execution_id)
            mapped_upload = await _await_upload(str(mapped_execution_id), remaining(), poll_interval_seconds)
            result["execution_status"] = mapped_upload["execution_status"]
            if mapped_upload["timed_out"]:
                result["outcome"] = "timed_out"
                return await handed_back(
                    result, {"execution_status": mapped_upload["execution_status"]},
                    "The culverts were mapped onto the culvert fields and are re-importing; "
                    "I am still waiting on it.",
                )
            if mapped_upload["execution_status"] == "failed" or mapped_upload["dataset_pk"] is None:
                result["outcome"] = (
                    "upload_failed" if mapped_upload["execution_status"] == "failed" else "no_dataset"
                )
                result.pop("retry_execution_id")  # retrying a failed import cannot help
                result["execution"] = mapped_upload["execution"]
                return await handed_back(
                    result, {"execution_status": mapped_upload["execution_status"]},
                    "The mapped culverts failed to re-import, so nothing was attached.",
                )
            leftovers.append({"dataset_pk": dataset_pk, "name": source_alternate, "reason": "raw_upload"})
            dataset_pk = mapped_upload["dataset_pk"]
            result["dataset_pk"] = dataset_pk

    listing = await client.get(f"/projects/{project_id}/{route}/")
    # A PLAIN JSON list on the input routes (no count/results wrapper).
    rows = listing if isinstance(listing, list) else listing.get("results", [])
    if not rows and kind == "culvert":
        rows = [await _create_default_culvert_row(project_id)]
        result["created_row"] = True
    result["row_ids"] = [row.get("id") for row in rows if isinstance(row, dict)]
    if not rows:
        result["outcome"] = "no_default_row"
        result["message"] = (
            f"project {project_id} has no {kind} row to attach to. The default "
            f"'{INPUT_LAYER_DEFAULT_TITLES[kind]}' row is seeded by the terrain import: run "
            "finalize_terrain_upload, wait for get_terrain to report ready, then ~30 s more."
        )
        return await handed_back(
            result, {"row_ids": []},
            f"The project has no {label} layer to attach to yet (its terrain is not imported).",
        )

    row = _pick_default_row(rows, kind)
    if kind == "culvert" and row.get("gn_layer") is None:
        # The layer factory (queued by the POST) re-saves gn_layer when it
        # finishes; a PATCH before then would be silently overwritten.
        row, _polls, _elapsed, factory_timed_out = await _poll_until(
            lambda: client.get(f"/projects/{project_id}/{route}/{row['id']}/"),
            lambda record: isinstance(record, dict) and record.get("gn_layer") is not None,
            remaining(),
            poll_interval_seconds,
        )
        if factory_timed_out:
            result["outcome"] = "timed_out"
            result["row_id"] = row.get("id") if isinstance(row, dict) else None
            return await handed_back(
                result, {"row_id": result["row_id"]},
                "The project's culvert layer is still being created; I am still waiting on it.",
            )
    # The layer this tool's own POST had the factory make (empty) and the PATCH
    # below replaces. A pre-existing row's previous layer is the user's own
    # data, so it is not reported as leftover.
    factory_pk = row.get("gn_layer") if result.get("created_row") else None
    body, status_code = await client.patch(
        f"/projects/{project_id}/{route}/{row['id']}/", json={"gn_layer": dataset_pk}
    )
    result.pop("retry_execution_id", None)
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
        result["dataset_alternate"] = await _dataset_alternate(dataset_pk)
    except HydrataAPIError as exc:
        result["dataset_alternate"] = None
        result["dataset_lookup_error"] = str(exc)
    alternate = result["dataset_alternate"]
    if kind == "culvert":
        if factory_pk is not None and factory_pk != dataset_pk:
            try:
                factory_name = await _dataset_alternate(factory_pk)
            except HydrataAPIError:
                factory_name = None  # best effort: the pk alone identifies it
            leftovers.append({"dataset_pk": factory_pk, "name": factory_name, "reason": "empty_factory_layer"})
        result["leftover_datasets"] = leftovers
        if leftovers:
            result["leftover_hint"] = (
                f"{len(leftovers)} dataset(s) are no longer used by the project (nothing was deleted); "
                "the user may delete them from their GeoNode dataset list if they wish."
            )
    summary = await _layer_summary(alternate)
    summary["row_id"] = row["id"]
    # TASK-3188's perform_update swaps the map's MapLayer but swallows a swap
    # failure and still answers 200 (W0 sweep, handover §7c) — the human would
    # open the link and see the OLD layer. Check the map actually carries it.
    summary["map_updated"] = await _map_carries_layer(project_id, alternate)
    count = summary.get("feature_count")
    say = (
        f"{label.capitalize()} attached ({count if count is not None else 'an unknown number of'} "
        f"feature(s)). Open the link and confirm it is the right {label}, in the right "
        "place; sign it off there."
    )
    if culvert_report is not None:
        summary["culvert_problems"] = len(result["culvert_problems"])
        if result["culvert_problems"]:
            say += (
                f" {len(result['culvert_problems'])} culvert(s) still need details filled in "
                "before a run can use them (e.g. the invert levels): open the link and complete "
                "them in the map."
            )
    if summary["map_updated"] is False:
        say += (
            f" WARNING: the project map does not show the new layer yet — the scenario will "
            f"use it, but the map still shows the old {label}."
        )
    return await handed_back(result, summary, say, layer=alternate)


async def _await_upload(execution_id: str, timeout_seconds, poll_interval_seconds) -> dict:
    """Poll a GeoNode upload to finished|failed (bounded) and resolve its dataset pk.

    Returns {execution, execution_status, polls, elapsed_seconds, timed_out,
    dataset_pk}; dataset_pk is None unless the import finished with a resource.
    """
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
    dataset_pk = None
    if not timed_out and status != "failed":
        dataset_pk = _first_resource_id(execution)
        if dataset_pk is None:
            # The e2e recipe's fallback (test_anuga_merewether.py:296-320): the
            # detail route carries the same output_params under `request`.
            detail = await client.get_from_origin(f"/api/v2/executionrequest/{execution_id}/")
            dataset_pk = _first_resource_id(detail.get("request") if isinstance(detail, dict) else None)
    return {"execution": execution, "execution_status": status, "polls": polls,
            "elapsed_seconds": elapsed, "timed_out": timed_out, "dataset_pk": dataset_pk}


async def _dataset_alternate(dataset_pk):
    """A GeoNode dataset's `alternate` (its WFS typename), or None."""
    detail = await client.get_from_origin(f"/api/v2/datasets/{dataset_pk}/")
    return (detail.get("dataset") or {}).get("alternate") if isinstance(detail, dict) else None


async def _create_default_culvert_row(project_id) -> dict:
    """POST the default 'Culvert 01' row (TASK-3641); its layer is made asynchronously."""
    body, _status_code = await client.post(
        f"/projects/{project_id}/{INPUT_LAYER_ROUTES['culvert']}/",
        json={"title": INPUT_LAYER_DEFAULT_TITLES["culvert"]},
    )
    if not isinstance(body, dict) or body.get("id") is None:
        raise ToolError(f"creating the project's 'Culvert 01' layer returned no row ({body!r})")
    return body


async def _map_carries_layer(project_id, alternate) -> bool | None:
    """True/False: does the project's base map list `alternate` as a map layer? None = unknown."""
    if not alternate:
        return None
    try:
        base_map = await _project_base_map(project_id)
        if base_map is None:
            return None
        data = await client.get_from_origin(
            f"/api/v2/maps/{base_map}/", params={"exclude[]": "*", "include[]": "maplayers"}
        )
        layers = ((data or {}).get("map") or {}).get("maplayers") or []
        return any(isinstance(layer, dict) and layer.get("name") == alternate for layer in layers)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# TASK-3204 (W1, epic 3200) — the two listing primitives. The 09-21 drive made
# 17 `file://` probes because nothing listed what a project already holds.
# ---------------------------------------------------------------------------
# WFS feature-count lookups list_inputs may make for the kinds whose wrapper
# serializer carries no has_features (friction, inflow, building): one GET per
# row, so bounded. Rows past the bound report has_features null.
LIST_INPUTS_MAX_FEATURE_LOOKUPS = 24
# list_time_series follows the paginated list at most this many pages.
LIST_TIME_SERIES_MAX_PAGES = 20


async def _feature_count(alternate) -> int | None:
    """WFS resultType=hits for one layer, or None (best effort)."""
    try:
        xml = await client.get_text_from_origin("/gs/ows", params={
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": alternate, "resultType": "hits",
        })
    except Exception:
        return None
    match = re.search(r'numberMatched="(\d+)"', xml)
    return int(match.group(1)) if match else None


@_tool("list_inputs")
async def list_inputs(
    project_id: Annotated[int, "The project ID"],
) -> str:
    """List every input layer row of a project, per kind, with whether it has features.

    For each of boundary, friction, inflow, rainfall, building, mesh_region and culvert:
    the rows (`row_id`, `title`, `gn_layer` — the dataset pk — and
    `dataset_alternate`, the `workspace:name` WFS typename) and `has_features`.
    `has_features` comes from the API for boundary, rainfall, mesh_region and culvert;
    for friction, inflow and building (whose records do not carry it) it is a
    WFS feature count, at most 24 lookups per call — past that, or when a
    lookup fails, it is null (unknown, not false). A row with no `gn_layer`
    has no layer yet (`has_features` false). Use the row ids with
    create_scenario; this is the listing primitive — no need to probe files.
    """
    lookups = 0
    kinds: dict = {}
    for kind, route in INPUT_LAYER_ROUTES.items():
        listing = await client.get(f"/projects/{project_id}/{route}/")
        rows = listing if isinstance(listing, list) else (listing or {}).get("results", [])
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            alternate = _alternate(row.get("gn_layer_name")) if row.get("gn_layer") else None
            entry = {
                "row_id": row.get("id"),
                "title": row.get("title"),
                "gn_layer": row.get("gn_layer"),
                "dataset_alternate": alternate,
            }
            if "has_features" in row:
                entry["has_features"] = row.get("has_features")
            elif alternate is None:
                entry["has_features"] = False
            elif lookups < LIST_INPUTS_MAX_FEATURE_LOOKUPS:
                lookups += 1
                count = await _feature_count(alternate)
                entry["feature_count"] = count
                entry["has_features"] = None if count is None else count > 0
            else:
                entry["has_features"] = None
            if kind == "boundary" and "has_external_feature" in row:
                entry["has_external_feature"] = row.get("has_external_feature")
            out.append(entry)
        kinds[kind] = out
    return json.dumps({"project_id": project_id, "inputs": kinds}, indent=2)


@_tool("list_time_series")
async def list_time_series(
    project_id: Annotated[int, "The project ID"],
) -> str:
    """List a project's time series (rain gauges, hydrographs, tides) — never their rows.

    Each series: `id`, `name` (a rainfall polygon binds to its gauge by this
    exact name), `series_type`, `units`, `timezone`, `row_count`,
    `first_timestamp` and `last_timestamp`. The data rows themselves are
    dropped (a project's list is megabytes); fetch one series with the REST API
    (GET /projects/<id>/time-series/<id>/) if you need its values.
    """
    series: list = []
    params: dict = {"page": 1, "page_size": 100}
    total = None
    for _ in range(LIST_TIME_SERIES_MAX_PAGES):
        page = await client.get(f"/projects/{project_id}/time-series/", params=params)
        rows = page if isinstance(page, list) else (page or {}).get("results", [])
        if isinstance(page, dict):
            total = page.get("count", total)
        for row in rows:
            if not isinstance(row, dict):
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            row_data = data.get("rowData") if isinstance(data.get("rowData"), list) else []
            first = row_data[0] if row_data and isinstance(row_data[0], dict) else {}
            last = row_data[-1] if row_data and isinstance(row_data[-1], dict) else {}
            series.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "series_type": row.get("series_type"),
                "units": row.get("units"),
                "timezone": row.get("timezone"),
                "row_count": len(row_data),
                "first_timestamp": first.get("timestamp"),
                "last_timestamp": last.get("timestamp"),
            })
        if not isinstance(page, dict) or not page.get("next"):
            break
        params = dict(params, page=params["page"] + 1)
    result = {"project_id": project_id, "count": total if total is not None else len(series),
              "series": series}
    if total is not None and total > len(series):
        result["truncated"] = True
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# TASK-3812 (W1, epic 3801) — flood observations.
#
# A flood observation layer (`/projects/<id>/flood-observations/`, hydrata
# TASK-3807) is a project-level obs_ Point layer; each observation is one
# FEATURE of it (`.../<layer>/observations/<fid>/`). The layer is never a build
# input, so there is no scenario attach: create the layer, then add, edit or
# delete its observations through the REST routes, which validate every field
# (subtype rules, UTC conversion of naive times in `entry_timezone`, the Gauge
# record series) and answer 400 with a `problems` map naming each bad field.
# Those rules live in ONE place — the API — and are not mirrored here; the
# tools only shape the batch, wait for the layer and keep photos to links.
#
# Photos (D10): MCP attaches a REFERENCE LINK only (`source: reference_only`,
# stored and never fetched). It never moves photo bytes: the upload path is the
# photo strip on the observation form in the map, which sniffs the format,
# enforces 25 MB x 10, offers EXIF time/place and handles public consent.
# ---------------------------------------------------------------------------
OBSERVATION_DATUMS = {
    "terrain": "Same as terrain",
    "AHD": "AHD",
    "EGM2008": "EGM2008",
    "ellipsoidal": "Ellipsoidal",
    "local": "Local datum (constant offset)",
}
OBSERVATION_PHOTO_UI_PATH = (
    "open the project map (ui_url), choose the observation in the Observations "
    "group and add the file in its photo strip (JPEG, PNG or HEIC, up to 25 MB, "
    "10 photos per observation)"
)
# Keys an agent might use to smuggle photo bytes or a file path onto an observation.
OBSERVATION_PHOTO_KEYS = frozenset({
    "photo", "photos", "file", "files", "image", "images", "photo_file", "photo_path",
    "photo_url", "photo_base64",
})
# One add_observations call POSTs one observation at a time; prod's /mcp/ proxy
# cuts a call at 300 s, so a batch is bounded well inside that.
ADD_OBSERVATIONS_MAX = 200


def _photo_refusal(what: str) -> ToolError:
    return ToolError(
        f"{what} MCP attaches photos as links only: call add_observation_photo_link with an "
        f"https:// reference_url. To upload a photo file, {OBSERVATION_PHOTO_UI_PATH}."
    )


def _refuse_photo_keys(fields: dict, where: str) -> None:
    """Refuse an observation body that tries to carry a photo (file, path or bytes)."""
    photo_keys = sorted(OBSERVATION_PHOTO_KEYS & set(fields))
    if photo_keys:
        raise _photo_refusal(f"{where} carries {', '.join(photo_keys)}.")


def _observation_layer_entry(layer: dict) -> dict:
    """The compact layer record every observation tool speaks."""
    return {
        "layer_id": layer.get("id"),
        "title": layer.get("title"),
        "gn_layer": layer.get("gn_layer"),
        "dataset_alternate": _alternate(layer.get("gn_layer_name")) if layer.get("gn_layer") else None,
        "vertical_datum": layer.get("vertical_datum"),
        "datum_offset_m": layer.get("datum_offset_m"),
        "has_features": layer.get("has_features"),
    }


async def _observation_layer(project_id, layer_id) -> dict:
    """GET the layer detail (404 for another project's layer) as the compact entry."""
    layer = await client.get(f"/projects/{project_id}/flood-observations/{layer_id}/")
    if not isinstance(layer, dict):
        raise ToolError(f"flood observation layer {layer_id} answered {layer!r}, not a layer record")
    return _observation_layer_entry(layer)


def _datum_words(layer: dict) -> str:
    label = OBSERVATION_DATUMS.get(layer.get("vertical_datum"), layer.get("vertical_datum") or "unknown datum")
    offset = layer.get("datum_offset_m")
    return f"{label}, offset {offset} m" if offset not in (None, 0) else label


@_tool("create_observation_layer")
async def create_observation_layer(
    project_id: Annotated[int, "The project ID"],
    title: Annotated[str, "Layer title, e.g. 'August 1998 survey'"],
    vertical_datum: Annotated[
        str,
        "The datum the layer's stages are on: terrain (default, 'Same as terrain'), AHD, "
        "EGM2008, ellipsoidal or local ('Local datum (constant offset)', needs datum_offset_m)",
    ] = "terrain",
    datum_offset_m: Annotated[
        float | None,
        "Metres ADDED to every stage on the layer to bring it onto the terrain datum. "
        "Required for vertical_datum=local; null means 0 otherwise",
    ] = None,
    timeout_seconds: Annotated[int, "How long to wait for the map layer (clamped to 280)"] = 120,
    poll_interval_seconds: Annotated[float, "Seconds between layer polls"] = 2.0,
) -> str:
    """Create a flood observation layer in a project and wait for its map layer.

    A flood observation layer holds real flood evidence (Flood marks, Timed
    levels, Gauge records, Dry points) that every historic run is compared
    with (Compare with observations). It is project-level: it is never a scenario input.
    POSTs /projects/<id>/flood-observations/, then polls the layer until its
    GeoServer layer exists (`ready`), usually a few seconds; until then
    add_observations refuses. On `timed_out`, call list_observations later.
    """
    if vertical_datum not in OBSERVATION_DATUMS:
        raise ToolError(f"vertical_datum {vertical_datum!r} is not one of {', '.join(OBSERVATION_DATUMS)}")
    if vertical_datum == "local" and datum_offset_m is None:
        raise ToolError(
            "vertical_datum=local needs datum_offset_m: the metres added to every stage to "
            "bring it onto the terrain datum"
        )
    body, status_code = await client.post(
        f"/projects/{project_id}/flood-observations/",
        json={"title": title, "vertical_datum": vertical_datum, "datum_offset_m": datum_offset_m},
    )
    if not isinstance(body, dict) or body.get("id") is None:
        return _post_result(body, status_code)
    layer_id = body["id"]
    poll_error = None
    try:
        detail, polls, elapsed, timed_out = await _poll_until(
            lambda: _observation_layer(project_id, layer_id),
            lambda layer: bool(layer.get("gn_layer")),
            timeout_seconds, poll_interval_seconds,
        )
    except (HydrataAPIError, ToolError) as exc:
        # The layer already exists: a failed poll must not hide its id (a re-call
        # would make a second layer). Report what the POST answered instead.
        detail, polls, elapsed, timed_out = _observation_layer_entry(body), 0, 0.0, False
        poll_error = str(exc)
    result = {**detail, "ready": bool(detail.get("gn_layer")), "timed_out": timed_out,
              "polls": polls, "elapsed_seconds": elapsed, "http_status": status_code}
    if poll_error:
        result["poll_error"] = poll_error
    if result["ready"]:
        say = (f"Flood observation layer '{detail['title']}' ({_datum_words(detail)}) is ready. "
               "Add observations with add_observations; open the link to see it in the "
               "Observations group of the map.")
    else:
        say = (f"Flood observation layer '{title}' was created (layer {layer_id}) but its map "
               "layer is still being made; check with list_observations in a few seconds "
               "before adding observations.")
    await _hand_back(
        result, project_id,
        {"layer_id": layer_id, "title": detail.get("title"), "vertical_datum": detail.get("vertical_datum"),
         "datum_offset_m": detail.get("datum_offset_m"), "ready": result["ready"]},
        say, layer=detail.get("dataset_alternate"),
    )
    return json.dumps(result, indent=2)


@_tool("list_observations")
async def list_observations(
    project_id: Annotated[int, "The project ID"],
    layer_id: Annotated[
        int | None, "A flood observation layer id; omit to list the project's layers instead"
    ] = None,
) -> str:
    """List a project's flood observation layers, or one layer's observations.

    Without layer_id: every layer (layer_id, title, dataset_alternate, vertical_datum,
    datum_offset_m, has_features). With layer_id: {layer, count, results} where each
    observation carries its `fid` (the key for update_observation / delete_observation),
    subtype + subtype_label, stage/depth, sigma, bound, the observed window in UTC,
    `datum` status (an `unknown` datum is not scored), `photo_count` and `problems`
    (fields drawn in the map that fail validation, reported, never fixed).
    """
    if layer_id is None:
        listing = await client.get(f"/projects/{project_id}/flood-observations/")
        rows = listing if isinstance(listing, list) else (listing or {}).get("results", [])
        layers = [_observation_layer_entry(row) for row in rows if isinstance(row, dict)]
        return json.dumps({"project_id": project_id, "layers": layers}, indent=2)
    body = await client.get(f"/projects/{project_id}/flood-observations/{layer_id}/observations/")
    if not isinstance(body, dict):
        body = {"response": body}
    return json.dumps(_elide_presigned({"project_id": project_id, **body}), indent=2)


@_tool("add_observations")
async def add_observations(
    project_id: Annotated[int, "The project ID"],
    layer_id: Annotated[int, "The flood observation layer id (create_observation_layer / list_observations)"],
    observations: Annotated[
        list[dict],
        "Observations, each the API write body: subtype (flood_mark | timed_level | "
        "gauge_record | dry_point), geometry {type: Point, coordinates: [lon, lat]} (or native "
        "x/y with srid), value_kind (stage | depth) + stage_m or depth_m, optional name, "
        "uncertainty_grade (H/M/L) or uncertainty_m, bound (exact | at_least | at_most), "
        "observed_start/observed_end (ISO 8601; naive times read in entry_timezone), "
        "mark_type, flood_name, source, licence, notes; a flood_mark or dry_point must say "
        "which flood it is: a date window (observed_start / observed_end) or flood_name "
        "(ask the user; never guess a date); a gauge_record needs "
        "data_timeseries_id (+ gauge_zero_m). At most 200 per call",
    ],
    srid: Annotated[
        int | None,
        "EPSG code for every geometry that does not carry its own `srid` (e.g. 28356 for "
        "MGA56 eastings/northings); omit for [lon, lat] (EPSG:4326)",
    ] = None,
    entry_timezone: Annotated[
        str | None,
        "IANA zone for naive observed_start/observed_end on rows that do not carry their own "
        "entry_timezone, e.g. 'Australia/Sydney'",
    ] = None,
) -> str:
    """Add flood observations (Flood mark, Timed level, Gauge record, Dry point) to a layer.

    POSTs each observation to /projects/<id>/flood-observations/<layer>/observations/
    in order. The API validates every field: a row it refuses (400) is listed in
    `rejected` with its `problems` and the rest still land; a refusal that is not
    about the row (403 not an Editor, 404, 409) stops the batch and is reported in
    `stopped`, with `not_sent_count`. Stages are on the LAYER's vertical datum.
    Photos are not part of an observation: attach a link with add_observation_photo_link.
    """
    if not isinstance(observations, list) or not observations:
        raise ToolError("observations must be a non-empty list of observation objects")
    if len(observations) > ADD_OBSERVATIONS_MAX:
        raise ToolError(
            f"{len(observations)} observations in one call; send at most {ADD_OBSERVATIONS_MAX} "
            "per call (the call must finish inside the proxy's 300 s)"
        )
    for index, row in enumerate(observations):
        if not isinstance(row, dict):
            raise ToolError(f"observations[{index}] must be an object")
        _refuse_photo_keys(row, f"observations[{index}]")
    layer = await _observation_layer(project_id, layer_id)
    if not layer["gn_layer"]:
        raise ToolError(
            f"flood observation layer {layer_id} is still being created (no map layer yet); "
            "try again in a few seconds"
        )
    path = f"/projects/{project_id}/flood-observations/{layer_id}/observations/"
    created, rejected, stopped = [], [], None
    for index, row in enumerate(observations):
        body = dict(row)
        geometry = body.get("geometry")
        if srid is not None and isinstance(geometry, dict) and "srid" not in geometry:
            body["geometry"] = {**geometry, "srid": srid}
        if entry_timezone and "entry_timezone" not in body:
            body["entry_timezone"] = entry_timezone
        try:
            answer, status_code = await client.post(path, json=body, raise_for_status=False)
        except HydrataAPIError as exc:
            # A 5xx / timeout mid-batch: keep what already landed (a whole-call error
            # would invite a re-send that duplicates every created observation).
            stopped = {"index": index, "name": row.get("name"), "http_status": None, "error": str(exc)}
            break
        answer = answer if isinstance(answer, dict) else {"response": answer}
        if status_code in (200, 201):
            entry = {"index": index, "fid": answer.get("fid"), "name": answer.get("name"),
                     "subtype": answer.get("subtype")}
            if answer.get("problems"):
                entry["problems"] = answer["problems"]
            created.append(entry)
        elif status_code == 400:
            rejected.append({"index": index, "name": row.get("name"), "http_status": status_code,
                             "problems": answer.get("problems", answer)})
        else:
            stopped = {"index": index, "name": row.get("name"), "http_status": status_code, "body": answer}
            break
    result = {"layer_id": layer_id, "dataset_alternate": layer["dataset_alternate"],
              "vertical_datum": layer["vertical_datum"], "datum_offset_m": layer["datum_offset_m"],
              "created_count": len(created), "rejected_count": len(rejected),
              "created": created, "rejected": rejected}
    if stopped is not None:
        result["stopped"] = stopped
        result["not_sent_count"] = len(observations) - stopped["index"] - 1
    say = (f"{len(created)} flood observation(s) added to '{layer['title']}' "
           f"({_datum_words(layer)}).")
    if rejected:
        say += f" {len(rejected)} were refused; each is listed with the field that is wrong."
        # TASK-3847: a Flood mark / Dry point must say which flood it is.
        if any(isinstance(r.get("problems"), dict) and {"observed_start", "flood_name"} <= set(r["problems"])
               for r in rejected):
            say += (" A Flood mark or Dry point must say which flood it is: ask the user which flood "
                    "(its name, or the dates it was observed) and send flood_name or "
                    "observed_start / observed_end; never guess a date.")
    if stopped is not None:
        why = f"HTTP {stopped['http_status']}" if stopped["http_status"] else stopped["error"]
        say += (f" The batch stopped at row {stopped['index']} ({why}); that row and "
                f"{result['not_sent_count']} after it were not added.")
    say += " Open the link to check them on the map."
    await _hand_back(
        result, project_id,
        {"layer_id": layer_id, "title": layer["title"], "created_count": len(created),
         "rejected_count": len(rejected), "vertical_datum": layer["vertical_datum"]},
        say, layer=layer["dataset_alternate"],
    )
    return json.dumps(result, indent=2)


@_tool("update_observation")
async def update_observation(
    project_id: Annotated[int, "The project ID"],
    layer_id: Annotated[int, "The flood observation layer id"],
    fid: Annotated[int, "The observation's fid (from list_observations / add_observations)"],
    changes: Annotated[
        dict,
        "Only the fields to change, in the add_observations write shape (e.g. {\"stage_m\": 2.5} "
        "or a new geometry); validated by the API against the stored observation (a flood_mark "
        "or dry_point must keep a date window or a flood_name)",
    ],
) -> str:
    """Edit one flood observation (PATCH; only the fields given change).

    The API checks the stored observation merged with `changes` and refuses
    (400, every bad field named) rather than store a half-valid one. Photos are
    not fields: attach a link with add_observation_photo_link.
    """
    if not isinstance(changes, dict) or not changes:
        raise ToolError("changes must be a non-empty object of the fields to change")
    _refuse_photo_keys(changes, "changes")
    layer = await _observation_layer(project_id, layer_id)
    body, status_code = await client.patch(
        f"/projects/{project_id}/flood-observations/{layer_id}/observations/{fid}/", json=changes)
    result = {"layer_id": layer_id, "fid": fid, "observation": _elide_presigned(body),
              "http_status": status_code}
    name = body.get("name") if isinstance(body, dict) else None
    await _hand_back(
        result, project_id,
        {"layer_id": layer_id, "fid": fid, "changed_fields": sorted(changes)},
        f"Flood observation {name or fid} on '{layer['title']}' is updated "
        f"({', '.join(sorted(changes))}). Open the link to check it on the map.",
        layer=layer["dataset_alternate"],
    )
    return json.dumps(result, indent=2)


@_tool("delete_observation")
async def delete_observation(
    project_id: Annotated[int, "The project ID"],
    layer_id: Annotated[int, "The flood observation layer id"],
    fid: Annotated[int, "The observation's fid"],
) -> str:
    """Delete one flood observation and its photos (cannot be undone).

    DELETEs /projects/<id>/flood-observations/<layer>/observations/<fid>/; a
    missing fid is an error (404), not a silent success.
    """
    layer = await _observation_layer(project_id, layer_id)
    status_code = await client.delete(
        f"/projects/{project_id}/flood-observations/{layer_id}/observations/{fid}/")
    result = {"layer_id": layer_id, "fid": fid, "deleted": True, "http_status": status_code}
    await _hand_back(
        result, project_id, {"layer_id": layer_id, "fid": fid, "deleted": True},
        f"Flood observation {fid} on '{layer['title']}' is deleted, with its photos.",
        layer=layer["dataset_alternate"],
    )
    return json.dumps(result, indent=2)


@_tool("add_observation_photo_link")
async def add_observation_photo_link(
    project_id: Annotated[int, "The project ID"],
    layer_id: Annotated[int, "The flood observation layer id"],
    fid: Annotated[int, "The observation's fid"],
    reference_url: Annotated[str, "An http(s) link to the photo (stored, never fetched)"],
    caption: Annotated[str, "What the photo shows (optional, up to 500 characters)"] = "",
    credit: Annotated[str, "Who took it / the rights holder (optional)"] = "",
    public_consent: Annotated[
        bool, "Show this photo to visitors of a public project (default false: members only)"
    ] = False,
) -> str:
    """Attach a photo to a flood observation as a reference LINK (never a file).

    MCP never uploads photo bytes. A local path, file:// URL or data: URI is
    refused: to upload a photo file, open the observation in the map and use the
    photo strip on its form (JPEG, PNG or HEIC, 25 MB, 10 per observation).
    The link counts toward the 10.
    """
    link = (reference_url or "").strip()
    if not link.lower().startswith(("https://", "http://")):
        raise _photo_refusal(f"reference_url {link[:80]!r} is not an http(s) link.")
    if len(link) > 2000:
        raise ToolError("reference_url is longer than 2000 characters")
    layer = await _observation_layer(project_id, layer_id)
    body, status_code = await client.post(
        f"/projects/{project_id}/flood-observations/{layer_id}/observations/{fid}/photos/",
        json={"source": "reference_only", "reference_url": link, "caption": caption,
              "credit": credit, "public_consent": public_consent},
    )
    result = {"layer_id": layer_id, "fid": fid, "photo": _elide_presigned(body), "http_status": status_code}
    await _hand_back(
        result, project_id,
        {"layer_id": layer_id, "fid": fid, "reference_url": link, "public_consent": public_consent},
        f"Photo link added to flood observation {fid} on '{layer['title']}'"
        f"{' (shown to public visitors)' if public_consent else ' (members only)'}.",
        layer=layer["dataset_alternate"],
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# TASK-3816 (W2) + TASK-3844 (W3.5), epic 3801 — Compare with observations:
# read how a run compares with the project's flood observations, exclude /
# include an observation.
#
# Every historic run (clock_kind "historic") is compared with the project's
# flood observations worker-side; hydrata serves it at /runs/<id>/comparison/
# (headline, summary, gauges, datum_checks, one row per observation INCLUDING
# the unscored and excluded ones). D9: there is no PASS / FAIL — the headline
# is a set of figures at a tolerance (0.3 m default, presets 0.15 / 0.3 /
# 0.4), which is derived per request. A design or unlabelled run answers 404
# NO_COMPARISON: a normal answer ("this run has no comparison"), not an error
# to retry. Exclusions are logged per (run, layer, fid); an exclusion needs
# the engineer's reason, which a tool must never invent. TASK-3844: the epic
# compares; it does not calibrate or validate, so there is no role tool.
# ---------------------------------------------------------------------------
NO_COMPARISON_WORDS = (
    "this run has no comparison with observations (not a historic run: only runs whose every "
    "clocked forcing series is a recorded one are compared with flood observations)"
)


def _headline_sentence(headline) -> str | None:
    """TASK-3845 — the English headline from the API's numbers (the map words
    the same numbers per locale): '47 observations · 31 within ±0.30 m (66%) ·
    bias -0.12 m · RMSE 0.35 m · 1 excluded'. A sentence (an older server) or
    None passes through."""
    if not isinstance(headline, dict):
        return headline
    n, excluded = int(headline.get("n") or 0), int(headline.get("excluded") or 0)
    tol = f"±{float(headline.get('tolerance_m') or 0):.2f} m"
    noun = "observation" if n == 1 else "observations"
    parts = [f"{n} {noun}"]
    if n and headline.get("within_pct") is not None:
        parts.append(f"{int(headline.get('within') or 0)} within {tol} ({headline['within_pct']:.0f}%)")
    else:
        parts.append(f"{int(headline.get('within') or 0)} within {tol}")
    if n and headline.get("bias_m") is not None:
        parts.append(f"bias {headline['bias_m']:+.2f} m")
    if n and headline.get("rmse_m") is not None:
        parts.append(f"RMSE {headline['rmse_m']:.2f} m")
    parts.append(f"{excluded} excluded")
    needs_date = int(headline.get("needs_date") or 0)
    if needs_date:
        parts.append(f"{needs_date} {'needs' if needs_date == 1 else 'need'} a date")
    return " · ".join(parts)


def _comparison_path(run_id, tail: str = "") -> str:
    return f"/runs/{run_id}/comparison/{tail}"


def _elide_series(rows: list) -> None:
    """Replace each Gauge record's `modelled_series` (every saved frame) by its point count."""
    for row in rows:
        series = row.pop("modelled_series", None) if isinstance(row, dict) else None
        if isinstance(series, dict):
            row["modelled_series_points"] = len(series.get("time_s") or [])


def _comparison_refusal(body, status_code: int, what: str) -> ToolError:
    """A 4xx from a comparison route as a ToolError the agent can act on."""
    body = body if isinstance(body, dict) else {"response": body}
    if status_code == 404 and body.get("error_code") == "NO_COMPARISON":
        return ToolError(f"{what} refused: {NO_COMPARISON_WORDS}.")
    detail = body.get("problems") or body.get("detail") or body
    return ToolError(f"{what} refused: Hydrata API returned {status_code} — {json.dumps(detail)}")


async def _comparison_headline(run_id) -> tuple[str | None, str | None]:
    """(headline, error): the run's headline after a change, best effort."""
    try:
        body, status_code = await client.get_with_status(_comparison_path(run_id))
    except Exception as exc:  # the change already landed; never hide it behind a re-read
        return None, str(exc)
    if status_code != 200 or not isinstance(body, dict):
        return None, f"Hydrata API returned {status_code}"
    return _headline_sentence(body.get("headline")), None


@_tool("get_observation_comparison")
async def get_observation_comparison(
    run_id: Annotated[int, "The run ID (a historic run; others are not compared with observations)"],
    tolerance: Annotated[
        float | None,
        "Metres within which a residual counts as within (default: the project's saved tolerance, "
        "0.3 unless an Editor changed it; presets 0.15, 0.3, 0.4; above 0 and at most 5). "
        "Not stored: it only changes this answer",
    ] = None,
    layer_id: Annotated[int | None, "With fid: narrow the rows to ONE flood observation and add its history"] = None,
    fid: Annotated[int | None, "With layer_id: the observation's fid (list_observations)"] = None,
    include_series: Annotated[
        bool, "Keep each Gauge record's modelled stage series (every saved frame); default false"
    ] = False,
) -> str:
    """Compare a run with the project's flood observations: how well does it match them?

    Returns `headline` (the figures to tell the user, e.g. "47 observations · 31
    within ±0.30 m (66%) · bias -0.12 m · RMSE 0.35 m · 1 excluded"),
    `headline_numbers` (the same as numbers), `tolerance_m` (used) and
    `default_tolerance_m` (the project's saved one), `statistics`
    (the API's summary: n, within, within_pct, bias_m, rmse_m, weighted_rmse_m, mae_m, max_abs_m,
    excluded, not_scored by reason, gauges), `gauges` (Gauge records: peak error,
    timing error, NSE), `datum_checks` (warn the user on a `mismatch` or `unknown`)
    and one row per observation: observed_m / modelled_m / residual_m (modelled -
    observed, + = model high), within, scored / not_scored_reason, match
    (`outside_run_window` = not this flood), excluded + exclusion {reason, by, at}.
    There is no pass or fail: report the figures. A run that is not historic has
    none (`has_comparison` false) — do not retry it.
    """
    if (layer_id is None) != (fid is None):
        raise ToolError("give both layer_id and fid to read one observation, or neither for the whole run")
    params = {"tolerance": tolerance} if tolerance is not None else None
    body, status_code = await client.get_with_status(_comparison_path(run_id), params=params)
    project_id, scenario_id = None, None
    if status_code == 404 and isinstance(body, dict) and body.get("error_code") == "NO_COMPARISON":
        project_id, scenario_id = await _run_project_and_scenario(run_id, None)
        result = {"run": run_id, "has_comparison": False, "error_code": "NO_COMPARISON",
                  "detail": body.get("detail"), "http_status": status_code}
        await _hand_back(
            result, project_id, {"run_id": run_id, "has_comparison": False},
            f"Run {run_id}: {NO_COMPARISON_WORDS}. A design storm run is not compared with "
            "flood observations.",
            panel="scenarios", scenario=scenario_id, run=run_id,
        )
        return json.dumps(result, indent=2)
    if status_code != 200 or not isinstance(body, dict):
        raise _comparison_refusal(body, status_code, f"get_observation_comparison for run {run_id}")
    result = {"has_comparison": True, **_elide_presigned(body)}
    # TASK-3845: the API's headline is numbers; keep them and say the English.
    result["headline_numbers"] = result.get("headline")
    result["headline"] = _headline_sentence(result.get("headline"))
    # The API's `summary` (the figures) would be overwritten by the hand-back's
    # own `summary` key, so it is relayed as `statistics`.
    result["statistics"] = result.pop("summary", None)
    rows = [row for row in result.get("rows") or [] if isinstance(row, dict)]
    if layer_id is not None:
        rows = [row for row in rows if row.get("layer") == layer_id and row.get("fid") == fid]
        if not rows:
            raise ToolError(
                f"run {run_id} has no row for layer {layer_id} fid {fid} in its comparison (check "
                "the ids with list_observations; an observation just added is compared a few seconds later)"
            )
        result["history"] = (await client.get(
            _comparison_path(run_id, "history/"), params={"layer": layer_id, "fid": fid})).get("history")
    if not include_series:
        _elide_series(rows)
    result["rows"] = rows
    result["row_count"] = len(rows)
    project_id, scenario_id = await _run_project_and_scenario(run_id, None)
    headline = result.get("headline") or "no figures"
    say = f"Run {run_id} compared with flood observations: {headline}."
    if layer_id is not None:
        row = rows[0]
        if row.get("scored") and row.get("residual_m") is not None:
            outcome = f"residual {row['residual_m']} m (modelled - observed)"
        else:
            outcome = f"not scored ({row.get('not_scored_reason') or 'no residual'})"
        say += (f" {row.get('subtype_label') or 'Observation'} {row.get('name') or fid}: {outcome}"
                + (" (excluded)" if row.get("excluded") else "") + ".")
    warnings = [check.get("message") for check in result.get("datum_checks") or []
                if isinstance(check, dict) and check.get("status") in ("mismatch", "unknown")
                and check.get("message")]
    if warnings:
        say += " Datum warning: " + " ".join(warnings)
    await _hand_back(
        result, project_id,
        {"run_id": run_id, "headline": result.get("headline"), "tolerance_m": result.get("tolerance_m"),
         "row_count": len(rows)},
        say + " Open the link to see the run in the map.",
        panel="scenarios", scenario=scenario_id, run=run_id,
    )
    return json.dumps(result, indent=2)


async def _comparison_write(run_id, tail: str, payload: dict, what: str) -> dict:
    body, status_code = await client.post(_comparison_path(run_id, tail), json=payload, raise_for_status=False)
    if status_code not in (200, 201) or not isinstance(body, dict):
        raise _comparison_refusal(body, status_code, what)
    result = {**body, "http_status": status_code}
    result["headline_after"], headline_error = await _comparison_headline(run_id)
    if headline_error:
        result["headline_error"] = headline_error
    return result


@_tool("set_observation_exclusion")
async def set_observation_exclusion(
    run_id: Annotated[int, "The run ID (a historic run)"],
    layer_id: Annotated[int, "The flood observation layer id"],
    fid: Annotated[int, "The observation's fid"],
    reason: Annotated[
        str,
        "WHY the engineer excludes it, in their words (required to exclude; never invent one — "
        "ask the user). Optional when including it again",
    ] = "",
    excluded: Annotated[bool, "true to exclude (default), false to include it again"] = True,
) -> str:
    """Exclude a flood observation from ONE run's comparison with observations, or include it again.

    Needs Editor (or above) on the project. The exclusion is logged with the user
    and reason (nothing is overwritten) and keyed on (run, layer, fid), so it
    survives a recompute. The figures change at once: `headline_after` is the
    run's headline with the change applied. To exclude, the user must give the
    reason: if they have not, ASK them; do not make one up.
    """
    reason = (reason or "").strip()
    if excluded and not reason:
        raise ToolError(
            "an exclusion needs the engineer's reason (e.g. 'mark on a fence, not the flood'): "
            "ask the user why this observation should be excluded, then call again with it. "
            "Nothing was changed."
        )
    result = await _comparison_write(
        run_id, "exclusions/", {"layer": layer_id, "fid": fid, "excluded": excluded, "reason": reason},
        f"set_observation_exclusion on run {run_id}",
    )
    project_id, scenario_id = await _run_project_and_scenario(run_id, None)
    if result.get("excluded"):
        say = (f"Flood observation {fid} is excluded from run {run_id}'s comparison "
               f"(reason: {result.get('reason') or reason}).")
    else:
        say = f"Flood observation {fid} is included again in run {run_id}'s comparison."
    if result.get("headline_after"):
        say += f" Now: {result['headline_after']}."
    await _hand_back(
        result, project_id,
        {"run_id": run_id, "layer_id": layer_id, "fid": fid, "excluded": result.get("excluded"),
         "reason": result.get("reason")},
        say, panel="scenarios", scenario=scenario_id, run=run_id,
    )
    return json.dumps(result, indent=2)


@_tool("set_comparison_tolerance")
async def set_comparison_tolerance(
    project_id: Annotated[int, "The project ID"],
    tolerance_m: Annotated[
        float, "The project's default tolerance in metres (above 0 and at most 5; presets 0.15, 0.3, 0.4)"
    ],
) -> str:
    """Save a project's default tolerance for comparing runs with flood observations.

    Needs Editor (or above). Every later get_observation_comparison (and the map's
    Compare with observations tab) uses it unless a tolerance is passed. Only
    save it when the user asks to change the project's default; to show the
    figures at another tolerance, pass `tolerance` to get_observation_comparison.
    """
    body, status_code = await client.patch(
        f"/projects/{project_id}/comparison-tolerance/", json={"tolerance_m": tolerance_m})
    result = {**(body if isinstance(body, dict) else {"response": body}), "http_status": status_code}
    saved = result.get("tolerance_m", tolerance_m)
    await _hand_back(
        result, project_id, {"project_id": project_id, "tolerance_m": saved},
        f"Project {project_id}'s default comparison tolerance is now ±{float(saved):.2f} m.",
        panel="scenarios",
    )
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
    # W0 simplify pass (epic 3200, Phase 1.7) — ONE elision site for every tool
    # that answers with a run's state, instead of get_scenario eliding and
    # build_scenario not. `error_message` / `user_message` / `status_detail`
    # are server-composed free text, and build_scenario additionally folds the
    # raw build POST body in under `build=`. Safe here and NOT in
    # `_post_result`: nothing that must keep a signed URL (presign) goes
    # through `_build_state`. Applied after `state.update(extra)` so the
    # caller's `**extra` is covered too.
    return _elide_presigned(state)


def _build_refusal(detail, reason: str) -> str:
    return json.dumps(
        _build_state(detail, outcome="refused", posted=False, reason=reason), indent=2
    )


# The server's pre-build admission refusals (api_v2 _pre_build_admission_refusal):
# what the human is told for each, and whether the link frames the boundary.
BUILD_REFUSAL_SAY = {
    "BOUNDARY_NO_EXTERNAL": (
        "The build was refused: no boundary edge is marked External, so the model has no "
        "outer perimeter. Open the link and fix it in the map — set the outer edges' "
        "Location to External (the map offers a one-click fix) — then I will build again."
    ),
    "MESH_REGION_UNITS_UNMARKED": (
        "The build was refused: the mesh-region resolutions are not marked as lengths in "
        "metres, so the mesh density is unknown. An operator must convert them (the "
        "command is in `detail`); nothing was spent."
    ),
    "MESH_TOO_LARGE": (
        "The build was refused: the mesh is above the server's ceiling. A coarser "
        "resolution or a smaller boundary is needed; nothing was spent."
    ),
}


async def _build_hand_back(result: dict, project_id, scenario_id, seen: dict) -> dict:
    """ui_url / summary / say_to_user for every build_scenario answer (TASK-3203)."""
    run_cost = None
    try:  # the actual-$ re-price lives only on the run record; best effort
        if result.get("run_id") is not None and result.get("outcome") == "built":
            run = await client.get(f"/runs/{result['run_id']}/")
            run_cost = run.get("mesh_actual_cost_estimate") if isinstance(run, dict) else None
    except Exception:
        run_cost = None
    summary = {
        "scenario_id": scenario_id,
        "outcome": result.get("outcome"),
        "mesh_triangle_count_estimate": result.get("mesh_triangle_count_estimate"),
        "mesh_triangle_count": result.get("mesh_triangle_count"),
        "mesh_actual_cost_estimate": run_cost,
    }
    error_code = result.get("error_code")
    boundary_layer = seen.get("boundary_layer")
    params: dict = {"panel": "scenarios", "scenario": scenario_id}
    refusal = seen.get("refusal")
    if refusal in ("no_boundary", "boundary_has_no_features"):
        params = {"panel": "inputs", "layer": boundary_layer}
        say = (
            "I cannot build yet: the scenario has no boundary with features. Open the link "
            "and draw it in the map (Inputs > Boundaries), or give me a boundary file to attach."
        )
    elif error_code in BUILD_REFUSAL_SAY:
        say = BUILD_REFUSAL_SAY[error_code]
        if error_code == "BOUNDARY_NO_EXTERNAL":
            params = {"panel": "inputs", "layer": boundary_layer}
    elif result.get("outcome") == "built":
        say = (
            f"The scenario is built: {_fmt_count(result.get('mesh_triangle_count'))} triangles "
            f"(estimated {_fmt_count(result.get('mesh_triangle_count_estimate'))}), about "
            f"{_fmt_usd(run_cost)} to run. Open the link, check the mesh and inputs, and tell "
            "me when you are happy for me to start the simulation."
        )
    elif result.get("outcome") == "refused":
        say = f"I did not build: {result.get('reason')}."
    elif result.get("outcome") == "timed_out":
        say = "The build is still running; I am still waiting on it."
    elif result.get("outcome") == "error":
        say = f"The build failed: {result.get('error_message') or result.get('user_message')}"
    elif "http_status" in result and result.get("http_status", 0) >= 400:
        say = f"The build was refused by the server: {result.get('detail') or result}"
    else:
        say = f"The scenario's latest run is {result.get('outcome')}. Open the link to see it."
    return await _hand_back(result, project_id, summary, say, **params)


# ---------------------------------------------------------------------------
# Tool 16: create_scenario
# ---------------------------------------------------------------------------
@_tool("create_scenario")
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
    building: Annotated[int | None, "Building row id (optional)"] = None,
    mesh_region: Annotated[
        int | None,
        "MeshRegion row id (optional): finer meshing inside its polygons, each "
        "feature's `resolution` a LENGTH in metres (see the units note).",
    ] = None,
    culvert: Annotated[
        int | None,
        "Culvert row id (optional; attach_input_layer kind=culvert → row_id). The build "
        "refuses a culvert missing its shape, size or invert levels.",
    ] = None,
    description: Annotated[str, "Free-text description (optional)"] = "",
    structure: Annotated[
        int | None,
        "Deprecated alias of `building` (TASK-3586); ignored when `building` is given.",
    ] = None,
) -> str:
    """Create a DRAFT scenario (no build, no run) and report its mesh-triangle estimate.

    POSTs /projects/<id>/scenarios/ with the write fields, then GETs the
    scenario detail — the create response carries NO estimate; the detail's
    `mesh_triangle_count_estimate` (+ `_breakdown`) does. Returns a compact
    record (the same fields get_scenario answers with): id, name, the FK ids
    as stored, resolution, duration, `computed_status` (`created` = no run
    yet), the estimate and its breakdown, the price (`compute_cost_estimate`
    in USD, `vcpu_hours_estimate`), `inflow_anchor_mismatch`, and
    http_status. Next step: build_scenario (which asks for
    confirm=true above 100,000 triangles). Nothing is meshed or queued here.

    Units: `resolution` — on the SCENARIO and on every MeshRegion FEATURE — is
    a LENGTH in metres; ANUGA maximum_triangle_area = resolution²/2 (FloatField
    default 100, not nullable; 0 makes the estimate None), and the estimate
    prices both the same way (TASK-3186). The smallest value becomes the raster
    cell size. A MeshRegion feature must carry `resolution_units = m` (features
    drawn in the map are marked automatically); an unmarked one makes the build
    answer 422 MESH_REGION_UNITS_UNMARKED, naming the conversion an operator runs.

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
        "building": building if building is not None else structure,
        "mesh_region": mesh_region,
    }
    if culvert is not None:
        payload["culvert"] = culvert  # TASK-3641: sent only when given (older APIs lack the field)
    body, status_code = await client.post(f"/projects/{project_id}/scenarios/", json=payload)
    scenario_id = body.get("id") if isinstance(body, dict) else None
    if scenario_id is None:
        return _post_result(body, status_code)
    detail = await client.get(f"/projects/{project_id}/scenarios/{scenario_id}/")
    if not isinstance(detail, dict):
        return _post_result(body, status_code)
    # TASK-3187 (W0, epic 3200) — this tuple moved to SCENARIO_RECORD_KEYS so
    # get_scenario answers with the SAME fields (+ the two cost keys and
    # inflow_anchor_mismatch); a field withheld by the serializer stays absent
    # rather than being fabricated as null.
    result = _scenario_record(detail)
    result["http_status"] = status_code
    estimate = result.get("mesh_triangle_count_estimate")
    cost = result.get("compute_cost_estimate")
    await _hand_back(
        result, project_id,
        {"scenario_id": scenario_id, "resolution": result.get("resolution"),
         "duration": result.get("duration"), "mesh_triangle_count_estimate": estimate,
         "compute_cost_estimate": cost, "vcpu_hours_estimate": result.get("vcpu_hours_estimate")},
        f"Draft scenario '{result.get('name', name)}' is set up: about "
        f"{_fmt_count(estimate)} triangles at {result.get('resolution')} m, estimated "
        f"{_fmt_usd(cost)} to run. Open the link to review its inputs before I build it.",
        panel="scenarios", scenario=scenario_id,
    )
    return json.dumps(result, indent=2)


def _fmt_count(value) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else "an unknown number of"


def _fmt_usd(value) -> str:
    return f"US${value:,.2f}" if isinstance(value, (int, float)) else "an unknown cost"


# ---------------------------------------------------------------------------
# Tool 17: build_scenario
# ---------------------------------------------------------------------------
@_tool("build_scenario")
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

    Units: `resolution` — on the SCENARIO and on every MeshRegion FEATURE — is
    a LENGTH in metres; ANUGA maximum_triangle_area = resolution²/2 (FloatField
    default 100, not nullable; 0 makes the estimate None), and the estimate
    prices both the same way (TASK-3186). The smallest value becomes the raster
    cell size. A MeshRegion feature must carry `resolution_units = m` (features
    drawn in the map are marked automatically); an unmarked one makes the build
    answer 422 MESH_REGION_UNITS_UNMARKED, naming the conversion an operator runs.
    """
    hand_back: dict = {}  # filled by the spend gates: which boundary to point the human at
    result = json.loads(await _build_scenario_core(
        project_id, scenario_id, confirm, rebuild, timeout_seconds, poll_interval_seconds, hand_back,
    ))
    return json.dumps(await _build_hand_back(result, project_id, scenario_id, hand_back), indent=2)


async def _build_scenario_core(
    project_id, scenario_id, confirm, rebuild, timeout_seconds, poll_interval_seconds, hand_back,
) -> str:
    """build_scenario's steps 1-5 (its docstring); `hand_back` collects the boundary seen."""
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
        hand_back["refusal"] = "no_boundary"
        return _build_refusal(
            detail, "boundary has no features: the scenario has no boundary attached "
            "(attach_input_layer kind=boundary, then set it on the scenario)"
        )
    boundary = await client.get(f"/projects/{project_id}/boundaries/{boundary_id}/")
    if isinstance(boundary, dict):
        hand_back["boundary_layer"] = _alternate(boundary.get("gn_layer_name"))
    if not (isinstance(boundary, dict) and boundary.get("has_features")):
        hand_back["refusal"] = "boundary_has_no_features"
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
    hand_back["posted"] = True
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
