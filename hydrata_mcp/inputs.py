"""Input tools: time series, input-layer attach (incl. culvert mapping), list inputs and time series."""

import json
import time
import uuid
from datetime import datetime
from typing import Annotated

from fastmcp.exceptions import ToolError

from ._common import _data, _tool, client
from ._helpers import (
    POLL_DEFAULT_TIMEOUT_SECONDS,
    POLL_MAX_TIMEOUT_SECONDS,
    _alternate,
    _hand_back,
    _layer_summary,
    _poll_until,
    _post_result,
    _project_base_map,
    _status_of,
    _ui_url,
)
from .client import HydrataAPIError

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
# TASK-3213 (epic 3200 W3): the SAME units rule hydrata enforces on POST (TASK-3211,
# hydrology.serializers_v2.UNITS_BY_SERIES_TYPE): the engine reads a rainfall series as mm/hr and a flow series
# as m^3/s whatever the label says, so the label must agree with the type. Refused here first, before any POST.
SERIES_UNITS = {
    "hyetograph": ("mm/hr", "mm/h"),
    "hydrograph": ("m3/s", "m^3/s", "cumec"),
    "stage": ("m",),
}
SERIES_ENGINE_CONVENTION = {
    "hyetograph": "the engine reads a rainfall series as mm/hr",
    "hydrograph": "the engine reads a flow series as m^3/s",
    "stage": "the engine reads a stage series as metres",
}

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
    # terrain chain seeds NO culvert row: write_input_layer makes it on demand
    # (TASK-4052); attach_input_layer reports no_default_row without one.
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
            'data must be an object shaped {"rowData": [{"timestamp": "<ISO 8601>", '
            '"value": <number>}, ...]} — the API answers 500, not 400, to any other shape'
        )
    for index, row in enumerate(data["rowData"]):
        if not isinstance(row, dict) or not {"timestamp", "value"} <= set(row):
            raise ToolError(f"data.rowData row {index} must be an object with 'timestamp' and 'value' keys")
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
        'The rows: {"rowData": [{"timestamp": "1998-08-17T00:00:00", "value": 1.5}, '
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
    units: Annotated[
        str,
        "Units of the values. REQUIRED for hyetograph ('mm/hr' or 'mm/h'), hydrograph ('m3/s', 'm^3/s' "
        "or 'cumec') and stage ('m'): the engine reads those units whatever the label says, so a "
        "mismatch is refused before anything is sent. Free for generic.",
    ] = "",
    source: Annotated[str, "Where the data came from, e.g. a gauge id or agency (optional)"] = "",
    location_name: Annotated[str, "Human-readable location of the gauge (optional)"] = "",
    timezone: Annotated[str, "IANA timezone of the timestamps (default 'UTC'), e.g. 'Australia/Sydney'"] = "UTC",
    hold_last_value: Annotated[
        bool,
        "Set true ONLY when the last value is meant to be held after the series ends. A hydrograph or "
        "hyetograph whose last value is not 0 is refused (TRAILING_VALUE_HELD) unless this is true: end "
        "the series with a 0 row instead, unless holding is really intended.",
    ] = False,
    method: Annotated[
        str,
        "One short phrase saying how you got these numbers (e.g. 'read off the council hydrograph PDF', "
        "'scaled the 1% AEP peak'). Shown to the person who reviews the inputs.",
    ] = "",
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

    The units must match the series_type (see `units`), and a hydrograph or
    hyetograph must END at 0 unless `hold_last_value` is true. The result
    carries Hydrata's own `summary` of the series (peak, volume or depth,
    first and last stamp, trailing value): repeat those numbers to the user
    (`say_to_user` does) rather than computing your own.
    """
    if series_type not in SERIES_TYPES:
        raise ToolError(f"series_type {series_type!r} is not one of {', '.join(SERIES_TYPES)}")
    row_count = _validate_row_data(data)
    allowed = SERIES_UNITS.get(series_type)
    if allowed and units not in allowed:
        raise ToolError(
            f"A {series_type} series needs units {' or '.join(allowed)}, not {units!r}: "
            f"{SERIES_ENGINE_CONVENTION[series_type]} whatever its label says. Nothing was sent; "
            f"call again with units={allowed[0]!r}, or the right series_type for these numbers."
        )
    payload = {
        "name": name,
        "description": description,
        "source": source,
        "location_name": location_name,
        "timezone": timezone,
        "series_type": series_type,
        "units": units,
        "hold_last_value": hold_last_value,
        "data": data,
    }
    if method.strip():
        payload["authored_method"] = method.strip()  # TASK-4055: shown on the review screen
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
    server_summary = body.get("summary") if isinstance(body.get("summary"), dict) else None
    result["hold_last_value"] = body.get("hold_last_value", hold_last_value)
    say = (
        f"Time series '{result['name']}' ({result['series_type']}, {row_count} rows, "
        f"{first} to {last}, {result['units'] or 'no units'}) is saved. Open the link to "
        "check its shape in the Hydrology panel."
    )
    if server_summary:
        say = f"{_series_sentence(result, server_summary)} {say}"
    await _hand_back(
        result,
        project_id,
        {
            "series_id": result["id"],
            "name": result["name"],
            "series_type": result["series_type"],
            "units": result["units"],
            "row_count": row_count,
            "first_timestamp": first,
            "last_timestamp": last,
        },
        say,
        panel="hydrology",
    )
    if server_summary:
        result["series_summary"] = server_summary  # Hydrata's numbers, computed from the true timestamps
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# TASK-4056 (epic 4051 W1.5) — correct or remove a time series the agent made.
# ---------------------------------------------------------------------------
@_tool("update_time_series")
async def update_time_series(
    project_id: Annotated[int, "The project ID"],
    series_id: Annotated[int, "The time series id (from list_time_series or create_time_series)"],
    name: Annotated[str, "New name (a rainfall polygon binds to its gauge by this exact name)"] = "",
    data: Annotated[
        dict | None,
        'Replacement rows: {"rowData": [{"timestamp": "<ISO 8601>", "value": <number>}, ...]}. '
        "Validated here before anything is sent; Hydrata refuses fewer than 2 rows, negative rain or flow "
        "values and an all-zero rain or flow series.",
    ] = None,
    description: Annotated[str, "New description"] = "",
    units: Annotated[str, "New units (must match the series_type, as in create_time_series)"] = "",
    series_type: Annotated[str, "New series_type: hyetograph, hydrograph, stage or generic"] = "",
    hold_last_value: Annotated[bool | None, "Set true only when the last value is meant to be held"] = None,
    method: Annotated[str, "One short phrase saying how you got the new numbers. Replaces the earlier account."] = "",
) -> str:
    """Correct a time series you created (PATCH; only the fields you pass change).

    Use it to fix a series instead of leaving a wrong one behind. The same
    rules as create_time_series apply to new rows (units match the type, a
    hydrograph or hyetograph ends at 0 unless `hold_last_value`, at least 2
    rows, no negative rain or flow, not all zero) and Hydrata refuses with the
    reason in plain words. A series already used by a built scenario changes the
    scenario: it needs a rebuild and the user reviews it again. Returns a compact
    record, not the rows.
    """
    changes: dict = {}
    if name:
        changes["name"] = name
    if description:
        changes["description"] = description
    if units:
        changes["units"] = units
    if series_type:
        if series_type not in SERIES_TYPES:
            raise ToolError(f"series_type {series_type!r} is not one of {', '.join(SERIES_TYPES)}")
        changes["series_type"] = series_type
    if hold_last_value is not None:
        changes["hold_last_value"] = hold_last_value
    if data is not None:
        _validate_row_data(data)
        changes["data"] = data
    if method.strip():
        changes["authored_method"] = method.strip()
    if not changes or set(changes) == {"authored_method"}:
        raise ToolError(
            "Nothing to change: pass at least one of name, data, description, units, series_type, hold_last_value."
        )
    body, status_code = await client.patch(f"/projects/{project_id}/time-series/{series_id}/", json=changes)
    if not isinstance(body, dict):
        return _post_result(body, status_code)
    landed = body.get("data")
    rows = landed.get("rowData") if isinstance(landed, dict) and isinstance(landed.get("rowData"), list) else []
    result = {
        "id": body.get("id", series_id),
        "name": body.get("name"),
        "series_type": body.get("series_type"),
        "units": body.get("units"),
        "timezone": body.get("timezone"),
        "row_count": len(rows),
        "authored_method": body.get("authored_method"),
        "http_status": status_code,
    }
    summary = body.get("summary") if isinstance(body.get("summary"), dict) else None
    await _hand_back(
        result,
        project_id,
        {
            "series_id": result["id"],
            "name": result["name"],
            "changed_fields": sorted(k for k in changes if k != "authored_method"),
        },
        f"Time series '{_data(result['name'])}' is updated ({', '.join(sorted(changes))}). Open the link to "
        "check its shape in the Hydrology panel; scenarios that use it need a rebuild.",
        panel="hydrology",
    )
    if summary:
        result["series_summary"] = summary
    return json.dumps(result, indent=2)


@_tool("delete_time_series")
async def delete_time_series(
    project_id: Annotated[int, "The project ID"],
    series_id: Annotated[int, "The time series id"],
) -> str:
    """Delete a time series (cannot be undone). Needs the Manager role on the project.

    A series still used by a feature of an inflow, rainfall or boundary layer is
    refused (409) and the refusal names the layer and the feature that use it:
    detach it there (or point that feature at another series), then call again.
    A series that no feature uses is deleted.
    """
    try:
        status_code = await client.delete(f"/projects/{project_id}/time-series/{series_id}/")
    except HydrataAPIError as exc:
        if "returned 409" in str(exc):
            raise HydrataAPIError(
                f"{exc} Nothing was deleted. Detach the series from that feature in the map "
                "(or point the feature at another series), then call delete_time_series again.",
                status=exc.status,
                code=exc.code,
            ) from exc
        raise
    result = {"id": series_id, "deleted": True, "http_status": status_code}
    await _hand_back(
        result,
        project_id,
        {"series_id": series_id, "deleted": True},
        f"Time series {series_id} is deleted.",
        panel="hydrology",
    )
    return json.dumps(result, indent=2)


# TASK-3213 AC5: a stage series belongs to a boundary feature, which hydrology_check does not list.
UNBOUND_CHECKED_SERIES_TYPES = ("hyetograph", "hydrograph")
# hydrology_check warnings after which "no feature uses this series" cannot be told from "the layer was unreadable"
UNBOUND_UNKNOWABLE_CODES = ("LAYER_UNREADABLE", "SERIES_NOT_FOUND")


async def _unbound_series(project_id, check) -> list:
    """The project's rainfall and flow series (`id`, `name`, `series_type`) that no feature in `check` uses.

    `check` is Hydrata's `hydrology_check` (per-feature `series`); [] when it is absent, unreadable, or its
    warnings say a layer or series could not be read (the answer would be a guess). Best effort: a failed
    series listing never breaks get_scenario.
    """
    if not isinstance(check, dict) or not isinstance(check.get("features"), list):
        return []
    if any(
        isinstance(w, dict) and w.get("error_code") in UNBOUND_UNKNOWABLE_CODES for w in check.get("warnings") or []
    ):
        return []
    used = {
        f["series"].get("id") for f in check["features"] if isinstance(f, dict) and isinstance(f.get("series"), dict)
    }
    try:
        listing = json.loads(await list_time_series(project_id))
    except Exception:  # noqa: BLE001 — a hint on top of the scenario; never fail the read for it
        return []
    return [
        {"id": row["id"], "name": row.get("name"), "series_type": row.get("series_type")}
        for row in listing.get("series") or []
        if isinstance(row, dict)
        and row.get("series_type") in UNBOUND_CHECKED_SERIES_TYPES
        and row.get("id") not in used
    ]


def _clock(stamp) -> str:
    """HH:MM of an ISO stamp (the server's, with an offset), else the stamp as given."""
    try:
        return datetime.fromisoformat(str(stamp)).strftime("%H:%M")
    except ValueError:
        return str(stamp)


def _series_sentence(result: dict, summary: dict) -> str:
    """'Hydrograph X: peak 19.7 m3/s at 02:00, volume 1.2e+05 m3, ends at 0.' from Hydrata's own summary."""
    kind = str(result.get("series_type") or "series").capitalize()
    parts = []
    peak = summary.get("peak_value")
    if isinstance(peak, (int, float)):
        unit = f" {result['units']}" if result.get("units") else ""
        parts.append(f"peak {peak:g}{unit} at {_clock(summary.get('peak_at'))}")
    total = summary.get("total")
    if isinstance(total, (int, float)):
        label = "volume" if summary.get("total_units") == "m3" else "depth"
        parts.append(f"{label} {total:.4g} {summary.get('total_units') or ''}".strip())
    trailing = summary.get("trailing_value")
    if isinstance(trailing, (int, float)):
        parts.append(f"ends at {trailing:g}" + (" (held on purpose)" if result.get("hold_last_value") else ""))
    return f"{kind} {result.get('name')}: {', '.join(parts)}."


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
        "(default 180, ceiling 200 — claude.ai cuts a tool call at 240 s). "
        "0 = one status read, no waiting.",
    ] = POLL_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
) -> str:
    """Attach a GeoJSON you uploaded to GeoNode as the project's boundary, friction, inflow, rainfall, building, mesh_region or culvert layer.

    Only for a client that has a shell and an agent key. A connector agent
    with no shell (claude.ai) writes the layer inline with write_input_layer,
    or, for a file above the inline cap, gives the user the
    request_input_upload link instead.

    The agent moves the bytes: first upload the GeoJSON yourself, with the
    same agent key, to GeoNode's upload endpoint at the site origin:

        curl -sS -H 'Authorization: Bearer hk_...' -F "base_file=@/path/rainfall.geojson" https://<site>/api/v2/uploads/upload/

    → JSON with `execution_id`. Then call this tool with it. The tool polls
    GET /api/v2/resource-service/execution-status/<execution_id> (bounded by
    timeout_seconds; statuses ready → running → finished | failed), reads the
    new dataset's pk, and PATCHes it onto the project's DEFAULT row of that
    kind ('Boundary 01' … 'MeshRegion 01' — the six rows the terrain import
    seeds ~30 s after get_terrain reports ready; if the list is still empty
    the tool says so: run finalize_terrain_upload / wait for get_terrain first).

    kind `culvert` (Hydraulic Structures; one two-point line per culvert,
    Inlet first, then Outlet): no culvert row is seeded by the terrain import,
    so a project with none answers `no_default_row`: write the culverts with
    write_input_layer, which makes the row. Hydrata converts every attached
    layer into the project's grid and the kind's table shape (single
    geometries, the_geom, the kind's own columns); attribute names are matched
    as they are, nothing is renamed for you (see write_input_layer).
    The culvert engine conveys flow through each culvert in a run (GPU
    lanes and localhost; the multi-core CPU lanes refuse a culvert scenario).

    `outcome` is exactly one of: `attached` (row_id, gn_layer, dataset_pk and
    dataset_alternate — the WFS typename — of the CONVERTED layer the row now
    uses, source_dataset_pk — the dataset you uploaded — and every row id seen);
    `timed_out` (still importing: call
    again with the same arguments, except pass `retry_execution_id` as
    execution_id when the result carries one); `upload_failed` (the
    execution record, incl. its log, is returned verbatim — fix the file and
    upload again); `no_default_row`; `no_dataset` (finished but no resource
    was recorded). The PATCH is refused with a 400 if the dataset is not
    owned by you.

    Every layer attached here is marked as written by your agent key and is
    UNREVIEWED until the user reviews it in their browser (the build refuses
    UNREVIEWED_INPUTS until then). INFLOWS: never invent an inflow location —
    no coordinates you made up, guessed or "placed plausibly". Import the
    location from the study's own files, or give the user the ui_url to draw
    it in the map; say in say_to_user that the inflow is unreviewed and where
    they check or draw it.

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
        await _hand_back(result, project_id, dict({"kind": kind}, **summary), say, panel="inputs", **params)
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
            result,
            {"execution_status": status},
            f"The {label} file failed to import; it needs fixing before it can be attached.",
        )

    dataset_pk = upload["dataset_pk"]
    result["dataset_pk"] = dataset_pk
    if dataset_pk is None:
        result["outcome"] = "no_dataset"
        result["execution"] = upload["execution"]
        return await handed_back(
            result,
            {"execution_status": status},
            f"The {label} upload finished but produced no layer, so nothing was attached.",
        )

    listing = await client.get(f"/projects/{project_id}/{route}/")
    # A PLAIN JSON list on the input routes (no count/results wrapper).
    rows = listing if isinstance(listing, list) else listing.get("results", [])
    result["row_ids"] = [row.get("id") for row in rows if isinstance(row, dict)]
    if not rows:
        result["outcome"] = "no_default_row"
        result["message"] = f"project {project_id} has no {kind} row to attach to. " + (
            "A culvert row is not seeded by the terrain import: write the culverts with "
            "write_input_layer (kind='culvert'), which creates the row."
            if kind == "culvert"
            else f"The default '{INPUT_LAYER_DEFAULT_TITLES[kind]}' row is seeded by the terrain import: run "
            "finalize_terrain_upload, wait for get_terrain to report ready, then ~30 s more."
        )
        return await handed_back(
            result,
            {"row_ids": []},
            f"The project has no {label} layer to attach to yet (its terrain is not imported).",
        )

    row = _pick_default_row(rows, kind)
    if kind == "culvert" and row.get("gn_layer") is None:
        # A culvert row made in the UI whose layer factory is still running
        # re-saves gn_layer when it finishes; a PATCH before then would be
        # silently overwritten.
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
                result,
                {"row_id": result["row_id"]},
                "The project's culvert layer is still being created; I am still waiting on it.",
            )
    body, status_code = await client.patch(
        f"/projects/{project_id}/{route}/{row['id']}/", json={"gn_layer": dataset_pk}
    )
    result.pop("retry_execution_id", None)
    gn_layer = body.get("gn_layer") if isinstance(body, dict) else None
    # TASK-3918 / TASK-4041: the v2 PATCH converts the upload into the kind's
    # canonical table and points the row (and the map) at THAT dataset, so the
    # row's gn_layer is what the scenario uses; the upload stays visible as
    # source_dataset_pk.
    attached_pk = gn_layer if gn_layer is not None else dataset_pk
    result.update(
        {
            "outcome": "attached",
            "row_id": row["id"],
            "row_title": row.get("title"),
            "gn_layer": gn_layer,
            "dataset_pk": attached_pk,
            "source_dataset_pk": dataset_pk,
            "http_status": status_code,
        }
    )
    # The layer's typename (workspace:name), the name the map link frames.
    # Best effort: the attach already happened.
    try:
        result["dataset_alternate"] = await _dataset_alternate(attached_pk)
    except HydrataAPIError as exc:
        result["dataset_alternate"] = None
        result["dataset_lookup_error"] = str(exc)
    alternate = result["dataset_alternate"]
    summary = await _layer_summary(alternate)
    summary["row_id"] = row["id"]
    # TASK-3749: whether the layer has features is the API's own answer (every
    # input wrapper serializer carries has_features); null when it is absent.
    has_features = body.get("has_features") if isinstance(body, dict) else None
    summary["has_features"] = has_features
    # TASK-3188's perform_update swaps the map's MapLayer but swallows a swap
    # failure and still answers 200 (W0 sweep, handover §7c) — the human would
    # open the link and see the OLD layer. Check the map actually carries it.
    summary["map_updated"] = await _map_carries_layer(project_id, alternate)
    if has_features is True:
        contents = "it has features"
    elif has_features is False:
        contents = "it has no features: check the file you uploaded"
    else:
        contents = "whether it has features is unknown"
    say = (
        f"{label.capitalize()} attached ({contents}). Open the link and confirm it is the "
        f"right {label}, in the right place; review it there."
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
    return {
        "execution": execution,
        "execution_status": status,
        "polls": polls,
        "elapsed_seconds": elapsed,
        "timed_out": timed_out,
        "dataset_pk": dataset_pk,
    }


async def _dataset_alternate(dataset_pk):
    """A GeoNode dataset's `alternate` (its WFS typename), or None."""
    detail = await client.get_from_origin(f"/api/v2/datasets/{dataset_pk}/")
    return (detail.get("dataset") or {}).get("alternate") if isinstance(detail, dict) else None


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
# TASK-4052 (W1.1, epic 4051) — write_input_layer: the features travel in the call.
#
# A claude.ai connector agent has no shell and no key, so the curl + hk_ upload
# path attach_input_layer needs is closed to it. The model writes a tool
# argument as output tokens (128K tokens ~ 450 KB of GeoJSON), so the practical
# inline limit is ~150 KB; above it the person uploads in the browser instead.
# ---------------------------------------------------------------------------
INLINE_LAYER_MAX_BYTES = 150_000
INLINE_LAYER_KINDS = tuple(INPUT_LAYER_ROUTES)
INLINE_WRITE_PATH = "/projects/{project_id}/input-layers/write/"


@_tool("write_input_layer")
async def write_input_layer(
    project_id: Annotated[int, "The project ID"],
    kind: Annotated[
        str,
        "Which input this is: boundary, friction, inflow, rainfall, building, mesh_region or "
        "culvert ('structure' is a deprecated alias of building).",
    ],
    features: Annotated[
        dict,
        "A GeoJSON FeatureCollection with every feature of this kind. Coordinates are read as "
        "EPSG:4326 (lon/lat) unless the collection declares a `crs` member; Hydrata converts them "
        "into the project's grid. Multi* geometries are split into single ones.",
    ],
    method: Annotated[
        str,
        "One short phrase saying how you made these features (e.g. 'traced from the council PDF', "
        "'digitised from the survey CSV'). Shown to the person who reviews the layer.",
    ] = "",
) -> str:
    """Write a project input layer (boundary, friction, inflow, rainfall, building, mesh_region or culvert) from GeoJSON you send in this call.

    No file, no key, no upload: the features go in `features`. Hydrata
    converts them into the project's grid and puts them in the project's
    default layer of that kind, replacing what it held (the old layer is kept,
    unlinked). Unlike attach_input_layer there is nothing to poll.

    Every feature must be the kind's geometry (lines for boundary, inflow and
    culvert; polygons for friction, rainfall, building and mesh_region); a
    feature of another geometry refuses the whole write and nothing changes.
    A culvert is one two-point line per culvert, Inlet first, then Outlet; its
    attributes are the culvert fields (name, shape, width_m, ... see the
    culvert guidance), nothing is renamed for you.

    Limit: about 150 KB of GeoJSON (a few thousand vertices). A bigger layer
    is refused before anything is sent: ask the person to upload it in their
    browser with a request_input_upload link instead.

    The layer is marked as written by your agent key and is UNREVIEWED until
    the user reviews it in their browser. INFLOWS: never invent an inflow
    location; take it from the study's files or give the user the ui_url to
    draw it.
    """
    kind = kind.strip().lower().replace("-", "_")
    deprecation = None
    if kind in LEGACY_INPUT_LAYER_KINDS:
        kind, deprecation = LEGACY_INPUT_LAYER_KINDS[kind], LEGACY_KIND_DEPRECATION
    if kind in UNSUPPORTED_INPUT_LAYER_KINDS or kind not in INLINE_LAYER_KINDS:
        raise ToolError(
            f"kind {kind!r} is not one of {', '.join(INLINE_LAYER_KINDS)} "
            f"(breakline is refused: {UNSUPPORTED_INPUT_LAYER_REASON})"
        )
    size = len(json.dumps(features, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > INLINE_LAYER_MAX_BYTES:
        raise ToolError(
            f"these features are {size // 1000} KB, over the {INLINE_LAYER_MAX_BYTES // 1000} KB limit for "
            "writing a layer in a tool call. Nothing was sent. Ask the person to upload the layer in their "
            f"browser: call request_input_upload(project_id={project_id}, kind={kind!r}) for the upload link and give it to them."
        )
    body, status_code = await client.post(
        INLINE_WRITE_PATH.format(project_id=project_id),
        json={"kind": kind, "features": features, "authored_method": method},
        raise_for_status=False,
    )
    if status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        raise ToolError(f"Hydrata refused the {kind} layer ({status_code}): {detail or body}")
    result = dict(body) if isinstance(body, dict) else {"body": body}
    result["outcome"] = "written"
    result["http_status"] = status_code
    if deprecation:
        result["deprecation"] = deprecation
    alternate = result.get("dataset_alternate")
    label = kind.replace("_", " ")
    summary = await _layer_summary(alternate)
    summary.update(kind=kind, row_id=result.get("row_id"), feature_count=result.get("feature_count"))
    say = (
        f"{label.capitalize()} written ({result.get('feature_count')} features). Open the link and "
        f"confirm it is the right {label}, in the right place; review it there."
    )
    return json.dumps(await _hand_back(result, project_id, summary, say, panel="inputs", layer=alternate), indent=2)


UPLOAD_PAGE_LIMIT_MB = 100  # the importer's per-file cap (dataset_upload_size)


@_tool("request_input_upload")
async def request_input_upload(
    project_id: Annotated[int, "The project ID"],
    kind: Annotated[
        str,
        "Which input the file is for: boundary, friction, inflow, rainfall, building, mesh_region or "
        "culvert ('structure' is a deprecated alias of building).",
    ],
) -> str:
    """Get the link where the PERSON uploads a big input layer in their browser.

    For a layer too big to send with write_input_layer (over about 150 KB):
    building footprints, land-use friction. You cannot move file bytes, so give
    the person `ui_url`. It opens the project's upload page for this kind; they
    drop a GeoJSON, a zipped shapefile or a GeoPackage (up to 100 MB), Hydrata
    converts it into the project's grid and attaches it to the kind's layer,
    replacing what it held. Nothing is created until they drop a file.

    A file the person drops is their own input: it is marked 'uploaded by the
    user' and needs no review step. Afterwards call list_inputs: the layer is
    there with that `authored_method`. A file of the wrong kind (polygons on an
    inflow) is refused on the page with a plain message. The person needs Editor
    access on the project.
    """
    kind = kind.strip().lower().replace("-", "_")
    deprecation = None
    if kind in LEGACY_INPUT_LAYER_KINDS:
        kind, deprecation = LEGACY_INPUT_LAYER_KINDS[kind], LEGACY_KIND_DEPRECATION
    if kind in UNSUPPORTED_INPUT_LAYER_KINDS or kind not in INLINE_LAYER_KINDS:
        raise ToolError(f"kind {kind!r} is not one of {', '.join(INLINE_LAYER_KINDS)}")
    base_map = await _project_base_map(project_id)
    ui_url = _ui_url(base_map, panel="input-upload", kind=kind)
    if ui_url is None:
        raise ToolError(
            f"project {project_id} has no map yet, so it has no upload page; "
            "upload a terrain first (presign_terrain_upload) and ask again."
        )
    label = kind.replace("_", " ")
    result: dict = {
        "outcome": "link",
        "project_id": project_id,
        "kind": kind,
        "ui_url": ui_url,
        "limit_mb": UPLOAD_PAGE_LIMIT_MB,
        "summary": {
            "kind": kind,
            "accepts": ["GeoJSON", "zipped shapefile", "GeoPackage"],
            "limit_mb": UPLOAD_PAGE_LIMIT_MB,
        },
        "say_to_user": (
            f"Open the link and drop your {label} file (GeoJSON, a zipped shapefile or a GeoPackage, up to "
            f"{UPLOAD_PAGE_LIMIT_MB} MB) on the page. Hydrata converts it into the project's grid and replaces "
            f"the project's {label} layer; it is your own input, so there is nothing to approve. Tell me when "
            "it is done and I will check it with list_inputs."
        ),
    }
    if deprecation:
        result["deprecation"] = deprecation
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# TASK-3204 (W1, epic 3200) — the two listing primitives. The 09-21 drive made
# 17 `file://` probes because nothing listed what a project already holds.
# ---------------------------------------------------------------------------
# list_time_series follows the paginated list at most this many pages.
LIST_TIME_SERIES_MAX_PAGES = 20


@_tool("describe_input_kinds")
async def describe_input_kinds(
    kind: Annotated[
        str,
        "One kind to describe (boundary, friction, inflow, rainfall, building, mesh_region or culvert); "
        "empty describes all seven",
    ] = "",
) -> str:
    """Say what each input layer kind looks like: geometry, properties, allowed values, a minimal example.

    Read this BEFORE you build GeoJSON for write_input_layer or an upload. Per kind it
    returns the `geometry` (LineString or Polygon), every property the layer reads with its
    `type`, whether it is `required` and its `allowed` values or `min`/`max`, the
    `required_one_of` groups (an inflow needs data_constant or data_timeseries_id), `notes`
    in plain words and a minimal `example` FeatureCollection. It is generated by Hydrata from
    the same definitions the build reads, so it is never out of date: do not read source code
    or guess column names.
    """
    wanted = kind.strip().lower().replace("-", "_")
    body = await client.get("/input-kinds/")
    if not isinstance(body, dict) or not isinstance(body.get("kinds"), dict):
        raise ToolError("Hydrata did not return the input kinds. Try again shortly.")
    kinds = body["kinds"]
    if wanted:
        if wanted not in kinds:
            raise ToolError(f"kind {kind!r} is not one of {', '.join(kinds)}")
        kinds = {wanted: kinds[wanted]}
    return json.dumps({"note": body.get("note"), "kinds": kinds}, indent=2)


@_tool("list_inputs")
async def list_inputs(
    project_id: Annotated[int, "The project ID"],
) -> str:
    """List every input layer row of a project, per kind, with whether it has features.

    For each of boundary, friction, inflow, rainfall, building, mesh_region and culvert:
    the rows (`row_id`, `title`, `gn_layer` — the dataset pk — and
    `dataset_alternate`, the `workspace:name` WFS typename), `authored_method`
    (the one-line account an agent gave of how it made the layer; null when
    nobody said) and `has_features`.
    `has_features` is the API's own answer for every kind. It is null only when
    a row's record does not carry it: treat null as unknown, not as empty. A row
    with no `gn_layer` has no layer yet (`has_features` false). Use the row ids with
    create_scenario; this is the listing primitive — no need to probe files.
    """
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
            if "authored_method" in row:  # TASK-4055: how an agent made it (null = nobody said)
                entry["authored_method"] = row.get("authored_method")
            # TASK-3749: never a /gs/ WFS count (refused for every agent key since
            # TASK-3728). Every wrapper serializer carries has_features (friction
            # and building TASK-3603, inflow TASK-3487); absent = unknown.
            if "has_features" in row:
                entry["has_features"] = row.get("has_features")
            elif alternate is None:
                entry["has_features"] = False
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
    exact name), `series_type`, `units`, `timezone`, `authored_method` (how an
    agent made it; null when nobody said), `row_count`,
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
            series.append(
                {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "series_type": row.get("series_type"),
                    "units": row.get("units"),
                    "timezone": row.get("timezone"),
                    "row_count": len(row_data),
                    "first_timestamp": first.get("timestamp"),
                    "last_timestamp": last.get("timestamp"),
                }
            )
            if "authored_method" in row:  # TASK-4055
                series[-1]["authored_method"] = row.get("authored_method")
        if not isinstance(page, dict) or not page.get("next"):
            break
        params = dict(params, page=params["page"] + 1)
    result = {"project_id": project_id, "count": total if total is not None else len(series), "series": series}
    if total is not None and total > len(series):
        result["truncated"] = True
    return json.dumps(result, indent=2)
