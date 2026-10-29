"""Flood-observation tools: layers, observations and photo links."""

import json
from typing import Annotated

from fastmcp.exceptions import ToolError

from ._common import _data, _tool, client
from ._helpers import _alternate, _elide_presigned, _hand_back, _poll_until, _post_result
from .client import HydrataAPIError

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
# TASK-3850 (operator grill Q5): two fixed words; otherwise ANY vertical datum
# in the world (an EPSG vertical CRS such as 'EPSG:5703', or the datum's own
# name), validated by the API. No national list.
OBSERVATION_DATUM_WORDS = {
    "terrain": "Same as terrain",
    "local": "Local datum (constant offset)",
}
OBSERVATION_PHOTO_UI_PATH = (
    "open the project map (ui_url), choose the observation in the Observations "
    "group and add the file in its photo strip (JPEG, PNG or HEIC, up to 25 MB, "
    "10 photos per observation)"
)
# Keys an agent might use to smuggle photo bytes or a file path onto an observation.
OBSERVATION_PHOTO_KEYS = frozenset(
    {
        "photo",
        "photos",
        "file",
        "files",
        "image",
        "images",
        "photo_file",
        "photo_path",
        "photo_url",
        "photo_base64",
    }
)
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
        # TASK-3875: the zone naive observation times are read in by default,
        # derived by the API from where the project is (None: no location).
        "default_timezone": layer.get("default_timezone"),
        "default_timezone_source": layer.get("default_timezone_source"),
    }


async def _observation_layer(project_id, layer_id) -> dict:
    """GET the layer detail (404 for another project's layer) as the compact entry."""
    layer = await client.get(f"/projects/{project_id}/flood-observations/{layer_id}/")
    if not isinstance(layer, dict):
        raise ToolError(f"flood observation layer {layer_id} answered {layer!r}, not a layer record")
    return _observation_layer_entry(layer)


def _datum_words(layer: dict) -> str:
    label = OBSERVATION_DATUM_WORDS.get(layer.get("vertical_datum"), layer.get("vertical_datum") or "unknown datum")
    offset = layer.get("datum_offset_m")
    return f"{label}, offset {offset} m" if offset not in (None, 0) else label


@_tool("create_observation_layer")
async def create_observation_layer(
    project_id: Annotated[int, "The project ID"],
    title: Annotated[str, "Layer title, e.g. 'August 1998 survey'"],
    vertical_datum: Annotated[
        str,
        "The datum the layer's stages are on: terrain (default, 'Same as terrain'), local "
        "('Local datum (constant offset)', needs datum_offset_m), or the vertical datum the "
        "levels were surveyed on: an EPSG vertical CRS (e.g. 'EPSG:5703') or the datum's own "
        "name (at most 32 characters). Ask the user; never assume one from the country",
    ] = "terrain",
    datum_offset_m: Annotated[
        float | None,
        "Metres ADDED to every stage on the layer to bring it onto the terrain datum. "
        "Required for vertical_datum=local; null means 0 otherwise",
    ] = None,
    timeout_seconds: Annotated[int, "How long to wait for the map layer (clamped to 200)"] = 120,
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
    vertical_datum = (vertical_datum or "").strip() or "terrain"
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
            timeout_seconds,
            poll_interval_seconds,
        )
    except (HydrataAPIError, ToolError) as exc:
        # The layer already exists: a failed poll must not hide its id (a re-call
        # would make a second layer). Report what the POST answered instead.
        detail, polls, elapsed, timed_out = _observation_layer_entry(body), 0, 0.0, False
        poll_error = str(exc)
    result = {
        **detail,
        "ready": bool(detail.get("gn_layer")),
        "timed_out": timed_out,
        "polls": polls,
        "elapsed_seconds": elapsed,
        "http_status": status_code,
    }
    if poll_error:
        result["poll_error"] = poll_error
    if result["ready"]:
        say = (
            f"Flood observation layer {_data(detail['title'])} ({_datum_words(detail)}) is ready. "
            "Add observations with add_observations; open the link to see it in the "
            "Observations group of the map."
        )
    else:
        say = (
            f"Flood observation layer {_data(title)} was created (layer {layer_id}) but its map "
            "layer is still being made; check with list_observations in a few seconds "
            "before adding observations."
        )
    await _hand_back(
        result,
        project_id,
        {
            "layer_id": layer_id,
            "title": detail.get("title"),
            "vertical_datum": detail.get("vertical_datum"),
            "datum_offset_m": detail.get("datum_offset_m"),
            "ready": result["ready"],
        },
        say,
        layer=detail.get("dataset_alternate"),
    )
    return json.dumps(result, indent=2)


@_tool("list_observations")
async def list_observations(
    project_id: Annotated[int, "The project ID"],
    layer_id: Annotated[int | None, "A flood observation layer id; omit to list the project's layers instead"] = None,
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
        "gauge_record | dry_point), name (required: a short label, e.g. the survey mark id), "
        "geometry {type: Point, coordinates: [lon, lat]} (or native "
        "x/y with srid), value_kind (stage | depth) + stage_m or depth_m, "
        "uncertainty_grade (H/M/L) or uncertainty_m, bound (exact | at_least | at_most), "
        "observed_start/observed_end (ISO 8601; naive times read in entry_timezone, by default "
        "the project's own zone), "
        "mark_type, flood_name, source, licence, notes; a flood_mark or dry_point must say "
        "which flood it is: a date window (observed_start / observed_end) or flood_name "
        "(ask the user; never guess a date); a gauge_record needs "
        "data_timeseries_id (+ gauge_zero_m). At most 200 per call",
    ],
    srid: Annotated[
        int | None,
        "EPSG code for every geometry that does not carry its own `srid` (the projected CRS "
        "its eastings/northings are in, e.g. a UTM zone); omit for [lon, lat] (EPSG:4326)",
    ] = None,
    entry_timezone: Annotated[
        str | None,
        "IANA zone for naive observed_start/observed_end on rows that do not carry their own "
        "entry_timezone; omit to use the project's zone (derived from where the project is, "
        "reported back as entry_timezone). Give it only when the times were read elsewhere",
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
            f"flood observation layer {layer_id} is still being created (no map layer yet); try again in a few seconds"
        )
    path = f"/projects/{project_id}/flood-observations/{layer_id}/observations/"
    # TASK-3875: no zone given -> the project's own (never a constant).
    if entry_timezone:
        zone = {"timezone": entry_timezone, "source": "given"}
    else:
        zone = {
            "timezone": layer.get("default_timezone"),
            "source": layer.get("default_timezone_source") if layer.get("default_timezone") else None,
        }
    entry_timezone = zone["timezone"]
    zone_filled = False
    created, rejected, stopped = [], [], None
    for index, row in enumerate(observations):
        body = dict(row)
        geometry = body.get("geometry")
        if srid is not None and isinstance(geometry, dict) and "srid" not in geometry:
            body["geometry"] = {**geometry, "srid": srid}
        if entry_timezone and "entry_timezone" not in body:
            body["entry_timezone"] = entry_timezone
            zone_filled = zone_filled or bool(body.get("observed_start") or body.get("observed_end"))
        try:
            answer, status_code = await client.post(path, json=body, raise_for_status=False)
        except HydrataAPIError as exc:
            # A 5xx / timeout mid-batch: keep what already landed (a whole-call error
            # would invite a re-send that duplicates every created observation).
            stopped = {"index": index, "name": row.get("name"), "http_status": None, "error": str(exc)}
            break
        answer = answer if isinstance(answer, dict) else {"response": answer}
        if status_code in (200, 201):
            entry = {
                "index": index,
                "fid": answer.get("fid"),
                "name": answer.get("name"),
                "subtype": answer.get("subtype"),
            }
            if answer.get("problems"):
                entry["problems"] = answer["problems"]
            created.append(entry)
        elif status_code == 400:
            rejected.append(
                {
                    "index": index,
                    "name": row.get("name"),
                    "http_status": status_code,
                    "problems": answer.get("problems", answer),
                }
            )
        else:
            stopped = {"index": index, "name": row.get("name"), "http_status": status_code, "body": answer}
            break
    result = {
        "layer_id": layer_id,
        "dataset_alternate": layer["dataset_alternate"],
        "vertical_datum": layer["vertical_datum"],
        "datum_offset_m": layer["datum_offset_m"],
        "created_count": len(created),
        "rejected_count": len(rejected),
        "created": created,
        "rejected": rejected,
        "entry_timezone": zone,
    }
    if stopped is not None:
        result["stopped"] = stopped
        result["not_sent_count"] = len(observations) - stopped["index"] - 1
    say = f"{len(created)} flood observation(s) added to {_data(layer['title'])} ({_datum_words(layer)})."
    if zone_filled:
        where = "from the project location" if zone["source"] == "project_location" else "as given"
        say += f" Times are in {zone['timezone']} ({where})."
    if rejected:
        say += f" {len(rejected)} were refused; each is listed with the field that is wrong."
        # TASK-3847: a Flood mark / Dry point must say which flood it is.
        if any(
            isinstance(r.get("problems"), dict) and {"observed_start", "flood_name"} <= set(r["problems"])
            for r in rejected
        ):
            say += (
                " A Flood mark or Dry point must say which flood it is: ask the user which flood "
                "(its name, or the dates it was observed) and send flood_name or "
                "observed_start / observed_end; never guess a date."
            )
    if stopped is not None:
        why = f"HTTP {stopped['http_status']}" if stopped["http_status"] else stopped["error"]
        say += (
            f" The batch stopped at row {stopped['index']} ({why}); that row and "
            f"{result['not_sent_count']} after it were not added."
        )
    say += " Open the link to check them on the map."
    await _hand_back(
        result,
        project_id,
        {
            "layer_id": layer_id,
            "title": layer["title"],
            "created_count": len(created),
            "rejected_count": len(rejected),
            "vertical_datum": layer["vertical_datum"],
        },
        say,
        layer=layer["dataset_alternate"],
    )
    return json.dumps(result, indent=2)


@_tool("update_observation")
async def update_observation(
    project_id: Annotated[int, "The project ID"],
    layer_id: Annotated[int, "The flood observation layer id"],
    fid: Annotated[int, "The observation's fid (from list_observations / add_observations)"],
    changes: Annotated[
        dict,
        'Only the fields to change, in the add_observations write shape (e.g. {"stage_m": 2.5} '
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
        f"/projects/{project_id}/flood-observations/{layer_id}/observations/{fid}/", json=changes
    )
    result = {"layer_id": layer_id, "fid": fid, "observation": _elide_presigned(body), "http_status": status_code}
    name = body.get("name") if isinstance(body, dict) else None
    await _hand_back(
        result,
        project_id,
        {"layer_id": layer_id, "fid": fid, "changed_fields": sorted(changes)},
        f"Flood observation {_data(name) if name else fid} on {_data(layer['title'])} is updated "
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
    status_code = await client.delete(f"/projects/{project_id}/flood-observations/{layer_id}/observations/{fid}/")
    result = {"layer_id": layer_id, "fid": fid, "deleted": True, "http_status": status_code}
    await _hand_back(
        result,
        project_id,
        {"layer_id": layer_id, "fid": fid, "deleted": True},
        f"Flood observation {fid} on {_data(layer['title'])} is deleted, with its photos.",
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
        json={
            "source": "reference_only",
            "reference_url": link,
            "caption": caption,
            "credit": credit,
            "public_consent": public_consent,
        },
    )
    result = {"layer_id": layer_id, "fid": fid, "photo": _elide_presigned(body), "http_status": status_code}
    await _hand_back(
        result,
        project_id,
        {"layer_id": layer_id, "fid": fid, "reference_url": link, "public_consent": public_consent},
        f"Photo link added to flood observation {fid} on {_data(layer['title'])}"
        f"{' (shown to public visitors)' if public_consent else ' (members only)'}.",
        layer=layer["dataset_alternate"],
    )
    return json.dumps(result, indent=2)
