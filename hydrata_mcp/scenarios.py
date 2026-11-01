"""Scenario tools: read, create and build scenarios."""

import json
from typing import Annotated

from fastmcp.exceptions import ToolError

from ._common import _data, _tool, client
from ._helpers import (
    INPUT_REVIEW_REFUSALS,
    POLL_DEFAULT_TIMEOUT_SECONDS,
    _alternate,
    _elide_presigned,
    _hand_back,
    _input_review_hand_back,
    _poll_until,
    _post_result,
    _quote_estimate_words,
    _run_quote_words,
    _status_of,
)
from .inputs import _unbound_series

# TASK-3187 (W0, epic 3200) — the scenario fields a COMPACT read keeps, on top
# of `_build_state`'s ten flat state keys. ONE tuple shared by get_scenario and
# create_scenario so the two cannot drift: there is no `list_scenarios` tool
# among the 17 and no other read path to a scenario record, so a session that
# did not create the scenario would otherwise be blind to what it is about to
# build (`resolution`, `duration`, the input FKs, the terrain) and what it
# will cost (`quote_estimate`, TASK-4101: the ONE pre-build price) — the inverse of
# "show your work before build and spend". `inflow_anchor_mismatch` is the
# record's only pre-build correctness warning (a timeseries inflow starting
# after model_start gets zero flow over the lead-in gap before its first
# sample: run_anuga run_utils.py fillna(0.0), TASK-2825) and has no other
# MCP path at all.
SCENARIO_RECORD_KEYS = (
    "id",
    "project",
    "name",
    "description",
    "terrain",
    "boundary",
    "friction",
    "inflow",
    "rainfall",
    "building",
    "mesh_region",
    "culvert",
    "resolution",
    "duration",
    "computed_status",
    "mesh_triangle_count_estimate",
    "mesh_triangle_count_estimate_breakdown",
    "latest_run_is_valid",
    # TASK-4095: the run settings (resolution, duration ...) changed since the newest build; [] = none
    "run_settings_changed",
    # TASK-4101 (D34): the ONE pre-build price {price, list_price, label: "estimate"}. The CPU vCPU-hour
    # figures (compute_cost_estimate, vcpu_hours_estimate) are NOT relayed: a customer never sees two prices.
    "quote_estimate",
    "inflow_anchor_mismatch",
    # TASK-3208: the inputs you wrote that the user has not reviewed yet (members only)
    "unreviewed_inputs",
    # TASK-3213: Hydrata's per-feature hydrology check (series, summary, gross volume, structured warnings),
    # passed through VERBATIM when the server sends it (its shape is TASK-3212's)
    "hydrology_check",
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
    `quote_estimate` (the ONE pre-build price in USD, GPU formula, labelled an
    estimate: `price` is what the person pays, `0.00` inside the daily free band, and
    `list_price` is the real figure), and
    `inflow_anchor_mismatch` (null unless an inflow series starts after the
    model does: the model gets zero flow from that series over the lead-in
    gap before its first sample), and
    `hydrology_check` (when Hydrata sends it: for each inflow and rainfall
    feature, the series that drives it, its peak and volume, and structured
    warnings — repeat those numbers to the user, never your own), and
    `unbound_series` with a `say_to_user` warning when the project holds a
    rainfall or flow series that no inflow or rainfall feature uses (the model
    ignores it: tell the user and ask which input it belongs to).

    The `computed_status` field (there is NO `status` key on a scenario) is
    derived from the latest run and will be one of: created, building, built,
    queued, computing, processing, complete, error, or cancelled — `created`
    also means no run exists yet. A scenario must be `built` before it can be
    run. `latest_run_is_valid` is false after any edit since the last build.
    `unreviewed_inputs` lists the inputs written by an agent key that the user
    has not reviewed at their current content (kind, layer_id, title,
    feature_count, the grant that wrote it, `authored_method` (the agent's own account of
    how it made the input, when it gave one), `reopened` when an edit undid an
    earlier input review): while it is not empty, build and run are refused
    UNREVIEWED_INPUTS. Only the user reviews, in the browser — poll this tool
    every 30-60 s after giving them the review link.
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
    state = _build_state(detail, **_scenario_record(detail))
    unbound = await _unbound_series(project_id, state.get("hydrology_check"))
    if unbound:
        # TASK-3213 AC5: a series the agent made that no inflow or rainfall feature uses is bound to nothing
        state["unbound_series"] = unbound
        names = ", ".join(f"'{row['name']}'" for row in unbound)
        state["say_to_user"] = (
            f"Time series {names} {'is' if len(unbound) == 1 else 'are'} not used by any inflow or rainfall "
            "feature, so the model ignores it. Say which input should use it, or the model will not "
            "carry the water you expect."
        )
    return json.dumps(state, indent=2)


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


def _build_refusal(detail, reason: str, **extra) -> str:
    return json.dumps(_build_state(detail, outcome="refused", posted=False, reason=reason, **extra), indent=2)


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
    if result.get("error_code") in INPUT_REVIEW_REFUSALS:
        return await _input_review_hand_back(result, project_id, scenario_id)
    run_quote = run_list_price = None
    try:  # the built run's quote (the figure the ledger debits) lives only on the run record; best effort
        if result.get("run_id") is not None and result.get("outcome") == "built":
            run = await client.get(f"/runs/{result['run_id']}/")
            if isinstance(run, dict):
                run_quote, run_list_price = run.get("quote"), run.get("quote_list_price")
    except Exception:
        run_quote = run_list_price = None
    summary = {
        "scenario_id": scenario_id,
        "outcome": result.get("outcome"),
        "mesh_triangle_count_estimate": result.get("mesh_triangle_count_estimate"),
        "mesh_triangle_count": result.get("mesh_triangle_count"),
        "quote": run_quote,
        "quote_list_price": run_list_price,
    }
    if "quote_estimate" in result:
        summary["quote_estimate"] = result["quote_estimate"]
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
            f"(estimated {_fmt_count(result.get('mesh_triangle_count_estimate'))}); the run's price is "
            f"{_run_quote_words(run_quote, run_list_price)}. Open the link, check the mesh and inputs, and tell "
            "me when you are happy for me to start the simulation."
        )
    elif result.get("outcome") == "refused" and refusal == "confirm_needed":
        say = (
            f"This scenario would build about {_fmt_count(result.get('mesh_triangle_count_estimate'))} triangles "
            f"(the mesh estimate), and its price is estimated at {_quote_estimate_words(result.get('quote_estimate'))}. "
            "I have not built it. Do you want me to build at this size? Say yes and I will, or give me a coarser "
            "resolution or a smaller boundary."
        )
    elif result.get("outcome") == "refused":
        say = f"I did not build: {result.get('reason')}."
    elif result.get("outcome") == "timed_out":
        say = "The build is still running; I am still waiting on it."
    elif result.get("outcome") == "error":
        say = f"The build failed: {_data(result.get('error_message') or result.get('user_message'))}"
    elif "http_status" in result and result.get("http_status", 0) >= 400:
        # A relayed refusal's detail can quote a person's text (422 CULVERT_INVALID names the
        # culvert), so the `detail` key is data too, not only its copy in say_to_user (TASK-3511).
        if isinstance(result.get("detail"), str):
            result["detail"] = _data(result["detail"])
        say = f"The build was refused by the server: {_data(str(result.get('detail') or result))}"
    else:
        say = f"The scenario's latest run is {result.get('outcome')}. Open the link to see it."
    return await _hand_back(result, project_id, summary, say, **params)


# ---------------------------------------------------------------------------
# Tool 16: create_scenario
# ---------------------------------------------------------------------------
def _scenario_panel(record: dict) -> str:
    """TASK-4093: the map panel a scenario's ui_url opens (the one place that decides).

    The one thing the person must do next, with an agent-written input unreviewed, is review it, so the
    link lands on the Review panel: the existing `panel=input-review` that the UNREVIEWED_INPUTS refusal
    already uses (epic 3200 D47: no new 'review' value, the machine name stays 'input-review'). With
    everything reviewed (or no agent input at all) it opens Scenarios, as before. create_scenario,
    update_scenario and clone_scenario all build their link through here, so they behave alike.
    """
    return "input-review" if record.get("unreviewed_inputs") else "scenarios"


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
    terrain: Annotated[int | None, "Terrain id (get_terrain → terrain.id); the build fails without one"] = None,
    boundary: Annotated[int | None, "Boundary row id (attach_input_layer kind=boundary → row_id)"] = None,
    friction: Annotated[int | None, "Friction row id (optional)"] = None,
    inflow: Annotated[int | None, "Inflow row id (optional; the build needs an inflow OR a rainfall)"] = None,
    rainfall: Annotated[int | None, "Rainfall row id (optional; the build needs an inflow OR a rainfall)"] = None,
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
    yet), the estimate and its breakdown, the price (`quote_estimate`:
    the ONE pre-build price in USD, labelled an estimate), `inflow_anchor_mismatch`, and
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
    quote_estimate = result.get("quote_estimate")
    await _hand_back(
        result,
        project_id,
        {
            "scenario_id": scenario_id,
            "resolution": result.get("resolution"),
            "duration": result.get("duration"),
            "mesh_triangle_count_estimate": estimate,
            "quote_estimate": quote_estimate,
        },
        f"Draft scenario {_data(result.get('name', name))} is set up: about "
        f"{_fmt_count(estimate)} triangles at {result.get('resolution')} m, and its price is estimated at "
        f"{_quote_estimate_words(quote_estimate)}. Open the link to review its inputs before I build it.",
        panel=_scenario_panel(result),
        scenario=scenario_id,
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# TASK-4104 (epic 4117 W1, D31): update_scenario and clone_scenario. Neither builds.
# ---------------------------------------------------------------------------
async def _edit_hand_back(project_id, scenario_id, detail, say) -> str:
    result = _scenario_record(detail)
    await _hand_back(
        result,
        project_id,
        {
            "scenario_id": scenario_id,
            "name": result.get("name"),
            "resolution": result.get("resolution"),
            "duration": result.get("duration"),
        },
        say,
        panel=_scenario_panel(result),
        scenario=scenario_id,
    )
    return json.dumps(result, indent=2)


@_tool("update_scenario")
async def update_scenario(
    project_id: Annotated[int, "The project ID"],
    scenario_id: Annotated[int, "The scenario ID"],
    name: Annotated[str | None, "New name. Keep it true: if it states a resolution or duration, change it too."] = None,
    description: Annotated[str | None, "New description"] = None,
    resolution: Annotated[
        float | None, "Mesh resolution as a LENGTH in metres (smaller = finer, more triangles)"
    ] = None,
    duration: Annotated[int | None, "Simulation duration in seconds"] = None,
    terrain: Annotated[int | None, "Terrain id to attach"] = None,
    boundary: Annotated[int | None, "Boundary row id to attach"] = None,
    friction: Annotated[int | None, "Friction row id to attach"] = None,
    inflow: Annotated[int | None, "Inflow row id to attach"] = None,
    rainfall: Annotated[int | None, "Rainfall row id to attach (write_input_layer with a target returns one)"] = None,
    building: Annotated[int | None, "Building row id to attach"] = None,
    mesh_region: Annotated[int | None, "MeshRegion row id to attach"] = None,
    culvert: Annotated[int | None, "Culvert row id to attach"] = None,
) -> str:
    """Edit a scenario: its name, description, resolution, duration, or which input rows it points at.

    A thin PATCH of scenarios/{id}/ with only the fields you pass. It NEVER
    builds or runs anything: a changed scenario is marked stale and you call
    build_scenario when the person agrees. Keep the name true: a scenario
    named '... - 10m' whose resolution you set to 30 must be renamed in the
    same call. Attaching an input row changes only THIS scenario; to vary an
    input for a variant, clone_scenario first, then attach the new row to the
    clone.
    """
    fields = {
        "name": name,
        "description": description,
        "resolution": resolution,
        "duration": duration,
        "terrain": terrain,
        "boundary": boundary,
        "friction": friction,
        "inflow": inflow,
        "rainfall": rainfall,
        "building": building,
        "mesh_region": mesh_region,
        "culvert": culvert,
    }
    payload = {key: value for key, value in fields.items() if value is not None}
    if not payload:
        raise ToolError("nothing to change: pass at least one of " + ", ".join(fields))
    base = f"/projects/{project_id}/scenarios/{scenario_id}/"
    await client.patch(base, json=payload)
    detail = await client.get(base)
    changed = ", ".join(sorted(payload))
    say = (
        f"Scenario {_data(str((detail or {}).get('name', scenario_id)) if isinstance(detail, dict) else scenario_id)} "
        f"updated ({changed}). Nothing was built or run; its earlier results no longer match it until it is "
        "built again."
    )
    return await _edit_hand_back(project_id, scenario_id, detail, say)


@_tool("clone_scenario")
async def clone_scenario(
    project_id: Annotated[int, "The project ID"],
    scenario_id: Annotated[int, "The scenario to copy"],
    name: Annotated[str | None, "Name for the copy (default: '<name>-copy'). Name it for what differs."] = None,
    overrides: Annotated[
        dict | None,
        "Fields of the copy to set at once, the same as update_scenario: description, resolution, duration, "
        "terrain, boundary, friction, inflow, rainfall, building, mesh_region, culvert.",
    ] = None,
) -> str:
    """Copy a scenario into a new sibling, for a variant. No run is copied and nothing is built.

    The copy points at the SAME input rows as the original. Give the variant
    its own input row by writing one with write_input_layer (target = a title)
    and passing the new row id in `overrides`; the original scenario's inputs
    stay as they were.
    """
    overrides = dict(overrides or {})
    allowed = {
        "description",
        "resolution",
        "duration",
        "terrain",
        "boundary",
        "friction",
        "inflow",
        "rainfall",
        "building",
        "mesh_region",
        "culvert",
    }
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        raise ToolError(f"overrides can set only: {', '.join(sorted(allowed))} (not {', '.join(unknown)})")
    body, _status = await client.post(f"/projects/{project_id}/scenarios/{scenario_id}/duplicate/")
    new_id = body.get("id") if isinstance(body, dict) else None
    if new_id is None:
        return _post_result(body, _status)
    payload = {key: value for key, value in overrides.items() if value is not None}
    if name:
        payload["name"] = name
    base = f"/projects/{project_id}/scenarios/{new_id}/"
    if payload:
        await client.patch(base, json=payload)
    detail = await client.get(base)
    say = (
        f"Copied scenario {scenario_id} to {new_id}. No run was copied and nothing was built. "
        "Open the link, check its inputs, then build it when the person agrees."
    )
    result_text = await _edit_hand_back(project_id, new_id, detail, say)
    return result_text


def _fmt_count(value) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else "an unknown number of"


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
        "(default 180, ceiling 200 — claude.ai cuts a tool call at 240 s). "
        "0 = one status read, no waiting.",
    ] = POLL_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
) -> str:
    """Build a scenario's package (mesh + inputs) after showing what it will cost, and poll until built.

    Needs a launch-level agent key (a build can start AWS Batch); a read or
    draft key gets 403 AGENT_LEVEL_INSUFFICIENT — the user creates a launch key
    on the site's Agent keys page (/agents/).

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
       100,000 triangles and confirm is not true — the refusal carries
       `mesh_triangle_count_estimate` and `quote_estimate` (the ONE pre-build price,
       labelled an estimate) and a `say_to_user` asking the person's yes: relay the
       number and the price and get the user's yes before confirm=true. confirm=true
       is a rule for you, not something the user does; never pass it on your own say-so. An estimate of 0 over a
       boundary WITH features is buildable and is reported, not refused.
    4. POST /projects/<id>/scenarios/<pk>/build/. Every 4xx comes back
       verbatim as a normal result with `http_status`: 422 MESH_TOO_LARGE
       (`error_code`, `estimate`, `ceiling`, `detail` — the server's hard
       ceiling; coarsen `resolution` or shrink the boundary; no run was
       created), 409 COMPUTE_TARGET_UNAVAILABLE, 400/403/404. 409
       UNREVIEWED_INPUTS (inputs you wrote that the user has not reviewed)
       answers `outcome` `unreviewed_inputs` with the `unreviewed_inputs` list and a
       `ui_url` to the scenario's review screen: give the user the link, poll
       get_scenario until its `unreviewed_inputs` is empty, then build again. A 409 WITHOUT
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
    result = json.loads(
        await _build_scenario_core(
            project_id,
            scenario_id,
            confirm,
            rebuild,
            timeout_seconds,
            poll_interval_seconds,
            hand_back,
        )
    )
    return json.dumps(await _build_hand_back(result, project_id, scenario_id, hand_back), indent=2)


async def _build_scenario_core(
    project_id,
    scenario_id,
    confirm,
    rebuild,
    timeout_seconds,
    poll_interval_seconds,
    hand_back,
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
    # TASK-4095: a changed run setting (resolution, duration ...) makes the built package stale even if
    # latest_run_is_valid still reads true: build again at the new settings
    package_is_current = detail.get("latest_run_is_valid") is not False and not detail.get("run_settings_changed")
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
            detail,
            "boundary has no features: the scenario has no boundary attached "
            "(attach_input_layer kind=boundary, then set it on the scenario)",
        )
    boundary = await client.get(f"/projects/{project_id}/boundaries/{boundary_id}/")
    if isinstance(boundary, dict):
        hand_back["boundary_layer"] = _alternate(boundary.get("gn_layer_name"))
    if not (isinstance(boundary, dict) and boundary.get("has_features")):
        hand_back["refusal"] = "boundary_has_no_features"
        return _build_refusal(
            detail,
            f"boundary has no features: Boundary {boundary_id} has_features is false "
            "(the server would admit the build and fail it in make_package) — upload the "
            "boundary GeoJSON and attach_input_layer kind=boundary first",
        )
    estimate = detail.get("mesh_triangle_count_estimate")
    if estimate is None:
        return _build_refusal(detail, "resolution is 0/unset: the mesh estimate is None")
    if estimate > BUILD_CONFIRM_ABOVE_TRIANGLES and not confirm:
        # TASK-4119 (D35): the refusal carries the triangle estimate AND the pre-build price (quote_estimate,
        # labelled estimate) and asks the PERSON's yes. confirm=true is a conduct rule for the agent, never a
        # human act: the agent relays the number and the price, gets the yes, and only then passes it.
        hand_back["refusal"] = "confirm_needed"
        quote_estimate = detail.get("quote_estimate")
        return _build_refusal(
            detail,
            f"estimate is {estimate:,} triangles, above {BUILD_CONFIRM_ABOVE_TRIANGLES:,}: "
            "building costs minutes on the web box and the run will need compute — "
            "coarsen `resolution` (a length in metres; triangles scale ~1/resolution²) "
            "or, once the user has said yes to the number and the price, call again with confirm=true "
            "to build at this size",
            quote_estimate=quote_estimate,
            needs_user_yes=True,
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
