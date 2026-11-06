"""Terrain tools: presign, finalize and read a terrain upload."""

import json
import math
from typing import Annotated

from fastmcp.exceptions import ToolError

from ._common import _data, _tool, client
from ._helpers import (
    POLL_DEFAULT_TIMEOUT_SECONDS,
    _alternate,
    _elide_presigned,
    _hand_back,
    _poll_until,
    _post_result,
    _status_of,
)
from .client import HydrataAPIError

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
        "Working CRS as an EPSG string. Hydrata accepts three families only: WGS84 UTM (EPSG:32601-32660 north, "
        "EPSG:32701-32760 south), GDA94 MGA (EPSG:28348-28358) and GDA2020 MGA (EPSG:7846-7859); national grids "
        "(British National Grid, US State Plane) are not supported yet. If the person's own data (survey, DEM, "
        "drawings) is in an MGA zone, use that MGA code (e.g. 'EPSG:7856', GDA2020 / MGA zone 56); otherwise use "
        "the WGS84 UTM zone of the site (e.g. 'EPSG:32756', WGS 84 / UTM 56S). If they mention their own data "
        "but not its grid, ask them before you create the project.",
    ],
) -> str:
    """Create a new ANUGA project. Returns the project record including its id, base_map and ui_url.

    The caller's own account becomes the owner. `projection` is the projected
    CRS every scenario in the project is meshed and run in: a WGS84 UTM, GDA94
    MGA or GDA2020 MGA code (any other grid is refused). Use the person's MGA
    zone when their own survey, DEM or drawings are in it, otherwise the site's
    WGS84 UTM zone; ask when it is unclear. The project's map is created alongside it; `ui_url`
    opens it — give it to the user. A project you create is unpublished
    (`is_published: false`): only its owner and members can see it until the
    user presses Publish in its Sharing panel in the browser — you cannot
    publish it. Next step for a new project: presign_terrain_upload.
    """
    body, status_code = await client.post("/projects/", json={"name": name, "projection": projection})
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
        result,
        project_id,
        {
            "project_id": project_id,
            "name": result.get("name", name),
            "projection": result.get("projection", projection),
            "base_map": result.get("base_map"),
        },
        f"Project {_data(result.get('name', name))} (id {project_id}, {projection}) is created. "
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
        "File size in bytes (`stat -c %s <file>`). The API rejects > 5 GiB up front. Omit if unknown.",
    ] = None,
) -> str:
    """Step 1 of a terrain import: get a presigned S3 PUT URL for a GeoTIFF DEM.

    Only for a client that has a shell and an agent key AND a DEM no platform
    source covers. A connector agent with no shell (claude.ai) uses
    list_terrain_sources then create_terrain_from_source instead, or asks the
    user to upload their own DEM in the map.

    The import needs a launch-level agent key: a draft key can presign, but
    finalize_terrain_upload (and the multipart fallback below) answer a read or
    draft key 403 AGENT_LEVEL_INSUFFICIENT — check the key before moving bytes.

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
    impossible: multipart-POST the file with the same agent key to
    /api/v2/anuga/projects/<id>/terrain/upload/ (form field `file`, optional
    `title`), which creates the terrain in one step — then get_terrain.
    """
    payload: dict = {"filename": filename, "content_type": content_type}
    if size is not None:
        payload["size"] = size
    body, status_code = await client.post(f"/projects/{project_id}/terrain/upload/presign/", json=payload)
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
    vertical_datum_declared: Annotated[
        str,
        "The datum the user SAYS the heights are on: `ahd` (Australian Height "
        "Datum), `orthometric_egm2008` or `ellipsoid`. Pass it only when the user told you; "
        "if they did not, ask them, do not guess. Stored as the terrain's declared datum, "
        "which beats Hydrata's own inference. Omit when unknown.",
    ] = "",
    levels_datum: Annotated[
        str,
        "The datum the PERSON'S levels (study, survey, tailwater) are on, e.g. `AHD`, "
        "`NAVD88`, `Ordnance Datum Newlyn` or an EPSG vertical code (`EPSG:5711`). A project fact, "
        "separate from the terrain's own datum. Pass it when the user told you; if they did not, "
        "ask them, do not guess.",
    ] = "",
) -> str:
    """Step 2 of a terrain import: register the PUT GeoTIFF as a Terrain and start the import.

    Needs a launch-level agent key (the import can start AWS Batch); a read or
    draft key gets 403 AGENT_LEVEL_INSUFFICIENT — the user creates a launch key
    on the site's Agent keys page (/agents/).

    Call this only after the presigned PUT returned 200. Creates the Terrain row
    (status `creating`) and queues the import chain — reproject to UTM, publish
    the layer + hillshade, style — which also seeds the project's six default
    boundary, friction, inflow, rainfall, building and mesh-region rows.
    Returns 202 with the terrain record; keep its `id` for get_terrain. A 400 UPLOAD_NOT_FOUND
    means no object is at `staging_key`: the PUT did not land (check its status
    code and Content-Type) — do not retry finalize until it has.
    """
    payload = {"staging_key": staging_key, "process_id": process_id, "title": title}
    declared = (vertical_datum_declared or "").strip().lower()
    if declared:
        if declared not in TERRAIN_DECLARED_DATUMS:
            raise ToolError(
                f"vertical_datum_declared must be one of {', '.join(TERRAIN_DECLARED_DATUMS)} "
                f"(got {vertical_datum_declared!r}). If the user has not said which datum the "
                "heights are on, ask them and leave this out."
            )
        payload["vertical_datum_declared"] = declared
    levels = (levels_datum or "").strip()
    if levels:
        payload["levels_datum"] = levels
    body, status_code = await client.post(
        f"/projects/{project_id}/terrain/upload/finalize/",
        json=payload,
    )
    result = json.loads(_post_result(body, status_code))
    await _hand_back(
        result,
        project_id,
        {"terrain_id": result.get("id"), "title": result.get("title") or title, "status": result.get("status")},
        f"The terrain upload is registered (terrain {result.get('id')}, {result.get('status')}). "
        "The import takes minutes; I will tell you when it is ready to check on the map.",
        panel="inputs",
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 13: get_terrain
# ---------------------------------------------------------------------------
TERRAIN_DECLARED_DATUMS = ("ahd", "orthometric_egm2008", "ellipsoid")


def _datum_name(value) -> str | None:
    """A datum as a short string: a plain string, a `describe()` dict, or an inference dict."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("value", "label", "name", "datum_guess"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return None


def _terrain_datum(record: dict) -> dict:
    """TASK-4061 (D9): `{declared, recorded, inferred, effective, effective_source}` for a terrain.

    Precedence recorded > declared > inferred. `recorded` is what a person recorded on the
    terrain (an Editor's entry); `declared` what was said at upload or by the terrain source;
    `inferred` is Hydrata's own guess from the raster, which is NOT a fact.
    """
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    recorded_row = record.get("recorded_vertical_datum")
    recorded = _datum_name(metadata.get("vertical_datum_recorded")) or (
        _datum_name(recorded_row) if isinstance(recorded_row, dict) and recorded_row.get("source") == "editor" else None
    )
    declared = _datum_name(metadata.get("vertical_datum_declared"))
    if declared is None and isinstance(recorded_row, dict) and recorded_row.get("source") != "editor":
        declared = _datum_name(recorded_row)  # a source stamp or embedded tag counts as declared
    inferred_raw = record.get("vertical_datum")
    inferred = None
    if isinstance(inferred_raw, dict):
        guess = _datum_name(inferred_raw.get("datum_guess"))
        if guess and guess != "unknown":  # 'unknown' = Hydrata could not tell, not a guess
            inferred = {
                "datum": guess,
                "confidence": inferred_raw.get("confidence"),
                "basis": inferred_raw.get("basis"),
            }
    elif _datum_name(inferred_raw):
        inferred = {"datum": _datum_name(inferred_raw), "confidence": None, "basis": None}
    if recorded:
        effective, source = recorded, "recorded"
    elif declared:
        effective, source = declared, "declared"
    elif inferred:
        effective, source = inferred["datum"], "inferred"
    else:
        effective, source = None, None
    return {
        "declared": declared,
        "recorded": recorded,
        "inferred": inferred,
        "effective": effective,
        "effective_source": source,
    }


def _basis_declared(record: dict) -> bool:
    raw = record.get("vertical_datum")
    return isinstance(raw, dict) and raw.get("basis") == "declared"


def _without_inferred_guess(record: dict) -> dict:
    """The terrain record without `inferred_datum_guess` / `inferred_confidence` (TASK-4118), top level and
    inside `vertical_datum`/`metadata.vertical_datum`."""
    drop = ("inferred_datum_guess", "inferred_confidence")
    cleaned = {k: v for k, v in record.items() if k not in drop}
    if isinstance(cleaned.get("vertical_datum"), dict):
        cleaned["vertical_datum"] = {k: v for k, v in cleaned["vertical_datum"].items() if k not in drop}
    metadata = cleaned.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("vertical_datum"), dict):
        cleaned["metadata"] = {
            **metadata,
            "vertical_datum": {k: v for k, v in metadata["vertical_datum"].items() if k not in drop},
        }
    return cleaned


@_tool("get_terrain")
async def get_terrain(
    project_id: Annotated[int, "The project ID"],
    terrain_id: Annotated[
        int | None,
        "The terrain ID from finalize_terrain_upload. Omit to follow the project's newest terrain.",
    ] = None,
    timeout_seconds: Annotated[
        int,
        "How long this call keeps polling before it returns `timed_out` "
        "(default 180, ceiling 200 — claude.ai cuts a tool call at 240 s). "
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
    datum = _terrain_datum(record)
    if record and (datum["effective_source"] in ("declared", "recorded") or _basis_declared(record)):
        # TASK-4118: the datum is a fact someone declared; Hydrata's own measured guess beside it is noise.
        record = _without_inferred_guess(record)
        result["terrain"] = record
    summary = {
        key: record.get(key)
        for key in (
            "id",
            "bbox_wgs84",
            "dem_elev_min",
            "dem_elev_max",
            "nodata_fraction",
            "native_crs",
            "native_resolution_m",
        )
    }
    result["vertical_datum"] = datum
    summary["vertical_datum"] = datum
    levels = record.get("levels_datum") if isinstance(record.get("levels_datum"), dict) else None
    mismatch = record.get("levels_datum_mismatch") if isinstance(record.get("levels_datum_mismatch"), str) else None
    result["levels_datum"] = levels
    summary["levels_datum"] = levels
    if mismatch:
        summary["levels_datum_mismatch"] = mismatch
    if outcome == "ready":
        nodata = record.get("nodata_fraction")
        nodata = f"{nodata:.1%}" if isinstance(nodata, (int, float)) else "an unknown share"
        say = (
            f"The terrain is ready: elevation {record.get('dem_elev_min')} to "
            f"{record.get('dem_elev_max')} m, {nodata} of cells without data, native CRS "
            f"{record.get('native_crs')}. Open the link and check it covers the whole site."
        )
        if datum["effective_source"] == "inferred":
            say += (
                f" Hydrata's own check guesses the heights are on {datum['effective']}, but that is "
                "a guess, not a fact. Ask the user which vertical datum their levels are in "
                "(for example AHD, EGM2008 or ellipsoidal) before you rely on it, and do not tell "
                "them it is confirmed."
            )
        elif datum["effective"]:
            say += f" Heights are on {datum['effective']} ({datum['effective_source']})."
        else:
            say += " No vertical datum is recorded: ask the user which datum their levels are in."
        if mismatch:
            say += f" WARNING: {_data(mismatch)} Tell the user, and do not mix the two sets of heights."
        elif levels is None:
            say += " The project has no levels datum yet: put the question of which datum their levels are on to the user, and pass levels_datum to create_terrain_from_source."
    elif outcome == "error":
        say = "The terrain import failed; the GeoTIFF needs fixing and uploading again."
    elif outcome == "not_found":
        say = "This project has no terrain yet."
    else:
        say = f"The terrain is still importing ({status}); I am still waiting on it."
    await _hand_back(
        result,
        project_id,
        summary,
        say,
        panel="inputs",
        layer=_alternate(record.get("gn_layer_name")) if outcome == "ready" else None,
    )
    # TASK-3187 — this tool composes its own envelope, so elide the COMPOSED
    # result; the terrain record carries no signed key today (future-proofing).
    return json.dumps(_elide_presigned(result), indent=2)


# ---------------------------------------------------------------------------
# TASK-4059 (W3.1, epic 4051) — Hydrata's own terrain sources.
#
# Wraps two EXISTING routes (no hydrata change): GET terrain/sources/?bbox= and
# POST terrain/create-from-bbox/. An agent that needs a DEM for a site asks
# Hydrata what it can fetch (GA 5 m LiDAR where it has data, Copernicus 30 m
# elsewhere) instead of pulling its own from a bucket with operator credentials.
# ---------------------------------------------------------------------------
def _bbox_args(bbox) -> list[float] | None:
    """`[min_lon, min_lat, max_lon, max_lat]` as four finite numbers, west < east, south < north, else None."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in bbox):
        return None
    if not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
        return None
    return [float(v) for v in bbox]


_BBOX_REFUSAL = (
    "bbox must be [min_lon, min_lat, max_lon, max_lat] in WGS84 degrees (4 numbers, "
    "west < east, south < north). Nothing was sent."
)


def _source_line(row: dict) -> str:
    cap = row.get("cap") or {}
    line = (
        f"{row.get('name')} ({row.get('resolution_m'):g} m, covers {row.get('coverage_pct'):.0f}% of the box, "
        f"heights on {row.get('vertical_datum')}, licence {row.get('licence')})"
        if isinstance(row.get("resolution_m"), (int, float)) and isinstance(row.get("coverage_pct"), (int, float))
        else f"{row.get('name')}"
    )
    if cap.get("state") != "ok":
        line += f" - too large: {cap.get('message')}"
    return line


@_tool("list_terrain_sources")
async def list_terrain_sources(
    project_id: Annotated[int, "The project ID (the key needs Contributor rights on it)"],
    bbox: Annotated[
        list[float],
        "The site as [min_lon, min_lat, max_lon, max_lat] in WGS84 degrees, e.g. "
        "[149.105, -35.285, 149.135, -35.255] for Sullivans Creek, Canberra.",
    ],
) -> str:
    """List the terrain Hydrata can fetch for a site, finest resolution first.

    Never fetch terrain from outside Hydrata (another DEM service or download)
    where a Hydrata source covers the site: call this first. Each source row has `key`,
    `name`, `resolution_m`, `coverage_pct` (share of your box it has data for),
    `cap` (`state` ok | too_large, the area and the maximum, and a plain `message`
    when too large), `vertical_datum`, `credit` and `licence`. Only sources with some
    coverage appear: Copernicus GLO-30 (30 m, worldwide) always, and finer regional
    LiDAR such as Geoscience Australia 5 m where it has data. Pick one whose `cap.state`
    is ok and pass its `key` to create_terrain_from_source. Tell the user which source
    you chose, its resolution, licence and datum (`say_to_user` says it for you).
    """
    box = _bbox_args(bbox)
    if box is None:
        raise HydrataAPIError(_BBOX_REFUSAL)
    data = await client.get(
        f"/projects/{project_id}/terrain/sources/", params={"bbox": ",".join(f"{v:g}" for v in box)}
    )
    rows = (data.get("sources") if isinstance(data, dict) else None) or []
    result: dict = {
        "outcome": "listed" if rows else "no_coverage",
        "bbox": box,
        "count": len(rows),
        "sources": rows,
    }
    usable = [row for row in rows if (row.get("cap") or {}).get("state") == "ok"]
    if not rows:
        say = "No terrain source Hydrata offers covers that area. Check the box is in WGS84 degrees, or upload your own GeoTIFF."
    elif not usable:
        say = (
            "Hydrata has terrain for that area but the box is too large for every source: "
            + "; ".join(_source_line(row) for row in rows)
            + ". Draw a smaller box."
        )
    else:
        best = usable[0]
        say = (
            f"Hydrata can fetch terrain for that area. Finest available: {_source_line(best)}. "
            + (f"Others: {'; '.join(_source_line(r) for r in rows if r is not best)}. " if len(rows) > 1 else "")
            + "Tell the user which source you would use and why, then call create_terrain_from_source."
        )
    summary = {
        "bbox": box,
        "sources": [
            {key: row.get(key) for key in ("key", "name", "resolution_m", "coverage_pct", "vertical_datum", "licence")}
            | {"cap_state": (row.get("cap") or {}).get("state")}
            for row in rows
        ],
    }
    await _hand_back(result, project_id, summary, say, panel="inputs")
    return json.dumps(result, indent=2)


@_tool("create_terrain_from_source")
async def create_terrain_from_source(
    project_id: Annotated[
        int,
        "The project ID (the key needs Contributor rights; a launch-level key since the import can start AWS Batch)",
    ],
    bbox: Annotated[list[float], "The site as [min_lon, min_lat, max_lon, max_lat] in WGS84 degrees"],
    source: Annotated[str, "A source `key` from list_terrain_sources, e.g. `ga_lidar_5m` or `copernicus_glo30`"],
    levels_datum: Annotated[
        str,
        "REQUIRED. The datum the PERSON'S levels (study, survey, tailwater) are on: `AHD`, `NAVD88`, "
        "`Ordnance Datum Newlyn`, or an EPSG vertical code such as `EPSG:5711`. Ask the user; "
        "never guess. Recorded on the project, NOT as the terrain's own datum.",
    ],
    title: Annotated[str, "Terrain title (default: the source name and box)"] = "",
    timeout_seconds: Annotated[
        int,
        "How long this call keeps polling the import before it returns `timed_out` (default 180, "
        "ceiling 200). 0 = start it and return at once; then call get_terrain.",
    ] = POLL_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
) -> str:
    """Have Hydrata fetch a terrain from one of its own sources, then wait for the import (like get_terrain).

    Never fetch terrain from outside Hydrata where a Hydrata source covers the site;
    if none does, the person uploads their own DEM in the project map.

    Use a `key` from list_terrain_sources. Hydrata fetches, reprojects and publishes the
    DEM and seeds the project's default input rows; the DEM's datum is stamped by the source.
    The result is get_terrain's envelope (`outcome` ready | error | timed_out, `terrain`,
    `polls`) plus `terrain_source`: the source's `key`, `name`, `resolution_m`, `licence`,
    `credit` and `vertical_datum`. `timed_out` is normal for a large area: keep calling
    get_terrain(project_id, terrain_id). A refusal (the box is over the source's size cap,
    the source has no data there, an unknown source) comes back as `outcome: refused`
    with the reason in `say_to_user` and nothing is created.
    """
    levels = (levels_datum or "").strip() if isinstance(levels_datum, str) else ""
    if not levels:
        raise ToolError(
            "levels_datum is required: the datum the user's levels are on (for example AHD, NAVD88, "
            "Ordnance Datum Newlyn or EPSG:5711). If the user has not said which datum their levels "
            "are in, ask them, do not guess."
        )
    box = _bbox_args(bbox)
    if box is None:
        raise HydrataAPIError(_BBOX_REFUSAL)
    payload: dict = {"source": source, "bbox": box, "levels_datum": levels}
    if title:
        payload["title"] = title
    body, status_code = await client.post(
        f"/projects/{project_id}/terrain/create-from-bbox/", json=payload, raise_for_status=False
    )
    body = body if isinstance(body, dict) else {"response": body}
    if status_code >= 400:
        if status_code >= 500:
            raise HydrataAPIError(f"POST create-from-bbox failed: {status_code} {body}", status=status_code)
        reason = body.get("detail") or body.get("error_code") or f"HTTP {status_code}"
        result = {
            "outcome": "refused",
            "http_status": status_code,
            "error_code": body.get("error_code"),
            "detail": _data(str(reason)),
        }
        await _hand_back(
            result,
            project_id,
            {"source": source, "bbox": box, "reason": result["detail"]},
            f"I did not fetch terrain: {_data(str(reason))} Nothing was created.",
            panel="inputs",
        )
        return json.dumps(result, indent=2)
    terrain_id = body.get("id")
    # One poll helper: get_terrain IS it (module functions stay plain callables).
    polled = json.loads(await get_terrain(project_id, terrain_id, timeout_seconds, poll_interval_seconds))
    # The registry row for what was fetched: resolution, licence, credit, datum. Best effort.
    row: dict = {}
    try:
        listing = await client.get(
            f"/projects/{project_id}/terrain/sources/", params={"bbox": ",".join(f"{v:g}" for v in box)}
        )
        row = next((r for r in (listing.get("sources") or []) if r.get("key") == source), {})
    except Exception as exc:
        polled["terrain_source_error"] = f"could not read the source details: {exc}"
    record = polled.get("terrain") if isinstance(polled.get("terrain"), dict) else {}
    terrain_source = {
        "key": source,
        "name": row.get("name"),
        "resolution_m": row.get("resolution_m"),
        "licence": row.get("licence"),
        "credit": row.get("credit"),
        "vertical_datum": row.get("vertical_datum") or record.get("recorded_vertical_datum"),
    }
    polled["terrain_source"] = terrain_source
    polled["terrain_id"] = terrain_id
    summary = dict(polled.get("summary") or {})
    summary.update(terrain_source=terrain_source, terrain_id=terrain_id)
    outcome = polled.get("outcome")
    credit = (
        f"Source: {terrain_source['name'] or source}, {terrain_source['resolution_m']} m, heights on "
        f"{terrain_source['vertical_datum']}, licence {terrain_source['licence']}."
    )
    if outcome == "ready":
        say = f"The terrain is ready. {credit} {polled.get('say_to_user', '')}".strip()
    elif outcome == "error":
        say = f"The terrain fetch failed. {credit} Try another source or upload a GeoTIFF."
    else:
        say = f"The terrain fetch is under way ({polled.get('status')}). {credit} I am still waiting on it."
    polled["summary"] = summary
    polled["say_to_user"] = say
    return json.dumps(_elide_presigned(polled), indent=2)


# ---------------------------------------------------------------------------
# TASK-4108 (W7e, epic 4117) - read the ground before placing anything on it.
#
# Two read-only wrappers over the SAME raster reads the Profile panel makes:
# GET terrain/<id>/profile/ (the panel's own route) and GET terrain/<id>/sample/. Hydrata answers `null`
# for nodata and for a point outside the raster; the tools keep that null (never 0).
# ---------------------------------------------------------------------------
SAMPLE_MAX_POINTS = 500
# W7e review F1: the sample route is a GET and nginx holds an 8 KB request line, which carries about 180
# full-precision points; the points go to Hydrata in chunks of this size, in order, so the 500 promise holds.
SAMPLE_CHUNK_POINTS = 100
# W7e review F8: a profile answer is one row per step; more than this in one reply floods the agent's context.
PROFILE_MAX_ROWS = 500
PROFILE_MAX_VERTICES = 150  # the line rides in a GET URL under nginx's 8 KB request line (MCP-6)
_EARTH_RADIUS_M = 6_371_000.0


def _line_length_m(pairs: list[list[float]]) -> float:
    """Great-circle length of a WGS84 polyline in metres (close enough to size the sample count)."""
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(pairs, pairs[1:]):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
        a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
        total += 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))
    return total


def _lon_lat_pairs(value, label: str, minimum: int) -> list[list[float]]:
    """`value` as a list of [lon, lat] pairs (WGS84), or a ToolError before any request."""
    if not isinstance(value, list) or len(value) < minimum:
        raise ToolError(f"{label} must be a list of at least {minimum} [lon, lat] pairs. Nothing was sent.")
    pairs = []
    for position, pair in enumerate(value, start=1):
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in pair)
        ):
            raise ToolError(f"{label}[{position}] must be [lon, lat] numbers. Nothing was sent.")
        lon, lat = float(pair[0]), float(pair[1])
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ToolError(
                f"{label}[{position}] is not WGS84: give [longitude, latitude] in degrees "
                "(longitude first). Nothing was sent."
            )
        pairs.append([lon, lat])
    return pairs


def _round_level(value):
    return round(value, 3) if isinstance(value, (int, float)) else None


@_tool("sample_terrain")
async def sample_terrain(
    project_id: Annotated[int, "The project ID"],
    terrain_id: Annotated[int, "The terrain ID (get_terrain, or the `terrain` on get_scenario)"],
    points: Annotated[
        list[list[float]],
        "Up to 500 [longitude, latitude] pairs in WGS84 degrees (longitude FIRST), e.g. [[149.1, -35.28]]",
    ],
) -> str:
    """Read the ground level of a terrain at points, to place a culvert, inflow or boundary on the ground.

    Returns `samples`: for each point you gave, in order, `lon`, `lat` and `level`
    (the terrain's own height in metres, on the terrain's vertical datum: see
    get_terrain `vertical_datum`). `level` is null where the terrain has no
    data or the point lies outside it: say so, never treat it as zero. These
    are the same values the browser's Profile panel reads. Read-only; changes nothing.
    """
    pairs = _lon_lat_pairs(points, "points", 1)
    if len(pairs) > SAMPLE_MAX_POINTS:
        raise ToolError(f"points holds at most {SAMPLE_MAX_POINTS} points (got {len(pairs)}). Nothing was sent.")
    rows = []
    for start in range(0, len(pairs), SAMPLE_CHUNK_POINTS):
        chunk = pairs[start : start + SAMPLE_CHUNK_POINTS]
        wkt = "MULTIPOINT(" + ", ".join(f"({lon!r} {lat!r})" for lon, lat in chunk) + ")"
        body = await client.get(f"/projects/{project_id}/terrain/{terrain_id}/sample/", params={"points": wkt})
        answered = body.get("samples", []) if isinstance(body, dict) else []
        if not isinstance(answered, list) or len(answered) != len(chunk):
            # Full review 03-11-26 (MCP-10): the 1:1 "for each point, in order" promise is checked, not assumed.
            raise HydrataAPIError(
                f"The terrain sampler answered {len(answered) if isinstance(answered, list) else 'no'} samples for "
                f"{len(chunk)} points; the result would not line up with your points, so nothing is returned."
            )
        rows.extend(answered)
    samples = [
        {"lon": row.get("lon"), "lat": row.get("lat"), "level": _round_level(row.get("terrain"))}
        for row in rows
        if isinstance(row, dict)
    ]
    levels = [row["level"] for row in samples if row["level"] is not None]
    missing = len(samples) - len(levels)
    summary = {
        "terrain_id": terrain_id,
        "points": len(samples),
        "with_level": len(levels),
        "without_level": missing,
        "level_min": min(levels) if levels else None,
        "level_max": max(levels) if levels else None,
    }
    say = f"Read the terrain at {len(samples)} point(s)."
    if levels:
        say += f" Ground levels run from {min(levels)} to {max(levels)} m."
    if missing:
        say += f" {missing} point(s) have no ground level (outside the terrain or no data): do not use them."
    return json.dumps({"samples": samples, "summary": summary, "say_to_user": say}, indent=2)


@_tool("terrain_profile")
async def terrain_profile(
    project_id: Annotated[int, "The project ID"],
    terrain_id: Annotated[int, "The terrain ID (get_terrain, or the `terrain` on get_scenario)"],
    line: Annotated[
        list[list[float]],
        "The line to read along: 2 to 150 [longitude, latitude] pairs in WGS84 degrees (longitude FIRST); "
        "simplify a longer line, it rides in the request URL",
    ],
    step_m: Annotated[
        float,
        "Metres between samples (default 5). One call returns at most 500 samples: use a larger step for a long line.",
    ] = 5.0,
) -> str:
    """Read the ground along a line (a cross-section) to find where a channel runs and snap a structure to it.

    Returns `profile`: samples ordered along the line, each with `distance_m`
    from the start, `lon`, `lat` and `level` (the terrain's height in metres,
    on its vertical datum: see get_terrain). `lowest` is the sample with the
    minimum level: across a channel it is the invert candidate, and its
    lon/lat is where the channel is. `level` is null where there is no data
    or the line leaves the terrain: those samples are never `lowest`. The same
    values the browser's Profile panel draws for the same line. Read-only.
    """
    pairs = _lon_lat_pairs(line, "line", 2)
    if len(pairs) > PROFILE_MAX_VERTICES:
        # Full review 03-11-26 (MCP-6): the line travels in the GET URL (nginx holds 8 KB, about 180 vertices).
        raise ToolError(
            f"line holds {len(pairs)} vertices; at most {PROFILE_MAX_VERTICES} per call. Simplify the line (keep "
            "its bends, drop the in-between vertices) or split it. Nothing was sent."
        )
    if isinstance(step_m, bool) or not isinstance(step_m, (int, float)) or not math.isfinite(step_m) or step_m <= 0:
        raise ToolError("step_m must be a number of metres greater than 0. Nothing was sent.")
    length_m = _line_length_m(pairs)
    expected = int(round(length_m / step_m)) + 1  # the backend rounds (MCP-9)
    if expected > PROFILE_MAX_ROWS:
        least = math.ceil(length_m / (PROFILE_MAX_ROWS - 1))
        raise ToolError(
            f"That line is about {round(length_m)} m long: at step_m {step_m} it would return {expected} samples, "
            f"more than the {PROFILE_MAX_ROWS} one call returns. Use step_m of at least {least} m, or a shorter "
            "line. Nothing was sent."
        )
    wkt = "LINESTRING(" + ", ".join(f"{lon!r} {lat!r}" for lon, lat in pairs) + ")"
    body = await client.get(
        f"/projects/{project_id}/terrain/{terrain_id}/profile/", params={"line": wkt, "spacing_m": step_m}
    )
    rows = body.get("samples", []) if isinstance(body, dict) else []
    profile = [
        {
            "distance_m": round(row["distance_m"], 2) if isinstance(row.get("distance_m"), (int, float)) else None,
            "lon": row.get("lon"),
            "lat": row.get("lat"),
            "level": _round_level(row.get("dem")),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    with_level = [row for row in profile if row["level"] is not None]
    lowest = min(with_level, key=lambda row: row["level"]) if with_level else None
    highest = max(row["level"] for row in with_level) if with_level else None
    length = profile[-1]["distance_m"] if profile else None
    # The spacing Hydrata used (it fits whole steps to the line), not only the one asked for.
    used = profile[1]["distance_m"] - profile[0]["distance_m"] if len(profile) > 1 else None
    summary = {
        "terrain_id": terrain_id,
        "samples": len(profile),
        "length_m": length,
        "step_m": round(used, 2) if isinstance(used, (int, float)) else step_m,
        "step_m_requested": step_m,
        "without_level": len(profile) - len(with_level),
        "level_min": lowest["level"] if lowest else None,
        "level_max": highest,
    }
    if lowest:
        say = (
            f"Read {len(profile)} ground levels along {length} m. The lowest is {lowest['level']} m at "
            f"{lowest['distance_m']} m along the line (lon {lowest['lon']}, lat {lowest['lat']}): that is the "
            "channel invert candidate if the line crosses a channel. Check it with the person."
        )
    else:
        say = "The line has no ground level anywhere: it lies outside the terrain or on no-data cells."
    if profile and len(with_level) < len(profile):
        say += f" {len(profile) - len(with_level)} sample(s) have no ground level."
    return json.dumps({"profile": profile, "lowest": lowest, "summary": summary, "say_to_user": say}, indent=2)
