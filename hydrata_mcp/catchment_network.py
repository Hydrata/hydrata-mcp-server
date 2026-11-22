"""Epic 4445 W11.4 - sub-catchments through four tools (D16: an agent edits; what overrules the terrain is a proposal).

create_sub_catchments  - creates sub-catchments from a High resolution line's catchments, by a target number (about 20 by
                         default) or a stream threshold, with forced points; or creates a set again. A fast-screen result is
                         refused with the engine's sentence ('High res only', D20). A terrain too large for the web server is
                         done on cloud compute: the answer is 'creating', and get_sub_catchments waits for it.
get_sub_catchments     - the sets of a line, or one set read compactly: nodes, sub-catchments, reaches and their facts (each
                         fact's definition, unit and source once, in `fact_definitions`), the 'can share water' notes, the
                         dragged boundaries and the proposals waiting for a person, and the marks ('The line has run again
                         since these were created.', 'Culverts have changed since these were created.'). Shapes stay on the
                         map; streams_of (W12.9, TASK-4575) gives the streams a divide goes on, as the map draws them.
edit_sub_catchments    - divide a sub-catchment, merge two, move a node, add a forced point, or undo a dragged boundary, on
                         the draft (an edit after a confirm starts a new draft). 'Make this a link' is filed as a PROPOSAL
                         (the API's proposals/ route): it changes nothing until a person accepts it on the map (D22, D16).
propose_sub_catchment_boundary - a dragged boundary between two sub-catchments, always filed WAITING: only a person keeps it as
                         drawn or makes the terrain agree. The sub-catchments cannot be confirmed while it waits.
propose_culvert_openings - (W11.5, TASK-4487) the one action for every flagged culvert: the drainage opening each flagged
                         culvert offers (the server's measured ladder, D15) is filed as a PROPOSAL, all in one request; a
                         person accepts them on the map, and only then is the terrain's drainage analysis worked out again.

Culverts (W11.5): get_sub_catchments reads each culvert placed in the sub-catchments with its area across the inlet, its
flags in the server's words, whether an opening is offered and the node it sits at; edit_sub_catchments' take_culverts_in
answers 'Culverts have changed since these were created.' (the draft made again with the culverts as they are now).

No tool confirms (a person does, in the browser) or stores a drainage barrier. Adopting the confirmed sub-catchments is part
of adopting a line's catchments (adopt_catchments). Words (Decision 889): sub-catchments, create and divide. The API's own
internal names (the 'v2-catchment-network-*' routes, process type network_cut) are never shown.
"""

import json
from typing import Annotated, Literal

from fastmcp.exceptions import ToolError

from ._common import _data, _tool, client
from ._helpers import POLL_DEFAULT_TIMEOUT_SECONDS, _hand_back, _poll_until
from .terrain import _lon_lat_pairs

SHAPES_STAY = (
    "Shapes (sub-catchment outlines, reaches) stay on the map; open the link to see them. get_sub_catchments' outlines_of "
    "gives the outlines of up to 10 sub-catchments."
)
CONFIRM_RULE = (
    "Sub-catchments are a draft until a person confirms them on the map. An agent divides, merges, moves a node, adds a "
    "forced point and proposes; it never confirms."
)
PROPOSAL_RULE = (
    "An agent only proposes what overrules the terrain (a dragged boundary, making a note a link). Nothing is decided "
    "until a person accepts it on the map."
)
DRAG_RULE = (
    "A boundary an agent drags is filed waiting. Only a person keeps it as drawn or makes the terrain agree (a drainage "
    "barrier a person stores), or undoes it. The sub-catchments cannot be confirmed while it waits."
)
OPENINGS_RULE = (
    "An agent only proposes the openings along flagged culverts. Nothing changes until a person accepts them on the map; "
    "only a person stores them with the terrain (its drainage analysis is then worked out again once)."
)
EDIT_KINDS = {
    "take_culverts_in": "Take the culverts in",
    "divide": "Divide a sub-catchment",
    "merge": "Merge two sub-catchments",
    "move_node": "Move a node",
    "add_forced_point": "Add a forced point",
    "undo_drag": "Undo a dragged boundary",
    "undo": "Undo an edit",
    "make_link": "Make this a link",
}
MAX_REASON = 200
MAX_OUTLINES = 10


def _path(project_id, set_id=None, tail="") -> str:
    base = f"/projects/{project_id}/sub-catchments/"
    return f"{base}{set_id}/{tail}" if set_id is not None else base


def _plural(n, one, many=None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _reason(reason) -> str:
    why = reason.strip() if isinstance(reason, str) else ""
    if not why or len(why) > MAX_REASON:
        raise ToolError(
            f"reason must be one or two sentences (1 to {MAX_REASON} characters) the person reads. Nothing was sent."
        )
    return why


def _compact_facts(facts, definitions: dict) -> dict:
    """{name: value}; each fact's unit, definition and source go once into ``definitions``."""
    out = {}
    for name, fact in (facts or {}).items():
        if isinstance(fact, dict):
            out[name] = fact.get("value")
            definitions.setdefault(
                name,
                {k: (_data(fact.get(k)) if k == "source" else fact.get(k)) for k in ("unit", "definition", "source")},
            )
        else:
            out[name] = fact
    return out


def _note(n: dict) -> dict:
    return {k: n.get(k) for k in ("id", "kind", "other", "rise_m", "within_accuracy", "lon", "lat")}


def _proposal(p: dict) -> dict:
    out = {k: p.get(k) for k in ("id", "kind", "label", "status", "note_id", "made_by_agent", "created_at")}
    out["note"] = _data(str(p.get("note") or ""))
    out["made_by"] = _data(p.get("made_by"))
    if p.get("decided_by"):
        out["decided_by"] = _data(p.get("decided_by"))
    inputs = p.get("inputs") or {}
    out["rise_m"] = inputs.get("rise_m")
    return out


def _sentences(version: dict) -> list[str]:
    """The marks and waits of a version, as sentences to relay."""
    out = []
    if version.get("line_changed_since"):
        out.append(" ".join(s for s in (version.get("line_changed_sentence"), version.get("fix_sentence")) if s))
        if version.get("create_again_sentence"):
            out.append(version["create_again_sentence"])
    if version.get("culverts_changed_since"):
        out.append(
            " ".join(s for s in (version.get("culverts_changed_sentence"), version.get("culverts_fix_sentence")) if s)
        )
    if version.get("forced_below_minimum_sentence"):
        out.append(version["forced_below_minimum_sentence"])
    waiting = [d for d in version.get("drags") or [] if d.get("state") == "waiting"]
    if waiting:
        out.append(
            f"{_plural(len(waiting), 'dragged boundary is', 'dragged boundaries are')} waiting for a person "
            "(Keep as drawn or Make the terrain agree, on the map); they cannot be confirmed until then."
        )
    culverts = version.get("culverts") if isinstance(version.get("culverts"), dict) else {}
    flagged, offered = culverts.get("flagged") or 0, culverts.get("openings_offered") or 0
    if flagged:
        words = f"{_plural(flagged, 'culvert is', 'culverts are')} flagged"
        if offered == 1:
            words += "; 1 offers a drainage opening along it: a person accepts or rejects it on the map"
        elif offered:
            words += f"; {offered} offer a drainage opening along them: a person accepts or rejects them on the map"
        out.append(words + ".")
    proposed = [p for p in version.get("proposals") or [] if p.get("status") == "proposed"]
    if proposed:
        out.append(
            f"{_plural(len(proposed), 'proposal is', 'proposals are')} waiting for a person to accept or reject on the map."
        )
    return out


def _rounded(geometry):
    """A GeoJSON geometry with its coordinates rounded to 6 places (about 0.1 m)."""
    if not isinstance(geometry, dict):
        return None

    def walk(value):
        if isinstance(value, list):
            return [walk(v) for v in value]
        return round(value, 6) if isinstance(value, float) else value

    return {"type": geometry.get("type"), "coordinates": walk(geometry.get("coordinates"))}


def _culvert_of(node: dict) -> dict | None:
    """The culvert a node is ({culvert, feature}), or None."""
    ref = node.get("source_ref") if isinstance(node.get("source_ref"), dict) else {}
    if ref.get("culvert") is None:
        return None
    return {"culvert": ref.get("culvert"), "feature": ref.get("feature")}


def _same(a: dict, b: dict) -> bool:
    return a.get("culvert") == b.get("culvert") and str(a.get("feature")) == str(b.get("feature"))


def _culverts(version: dict) -> dict:
    """The culverts placed in a version (W10.2's measured ladder), as an agent reads them: each with its area across the
    inlet (ha), its flags in the server's words, whether an opening along it is offered and the node it sits at; and the
    culverts not placed, with the reason."""
    block = version.get("culverts") if isinstance(version.get("culverts"), dict) else {}
    nodes = [n for n in version.get("nodes") or [] if isinstance(n, dict) and _culvert_of(n)]
    placed = []
    for c in block.get("placed") or []:
        node = next((n for n in nodes if _same(_culvert_of(n), c)), None)
        area = c.get("area_m2")
        placed.append(
            {
                "culvert": c.get("culvert"),
                "feature": c.get("feature"),
                "name": _data(c.get("name")),
                "node_id": node.get("id") if node else None,
                "node_kind": c.get("node_kind"),
                "on_line": bool(c.get("on_line")),
                "status": c.get("status"),
                "area_ha": round(area / 10_000, 4) if isinstance(area, (int, float)) else None,
                "flagged": bool(c.get("flagged")),
                "flags": [{"code": f.get("code"), "sentence": f.get("sentence")} for f in c.get("flags") or []],
                "opening_offered": bool(c.get("opening_offered")),
            }
        )
    not_placed = [
        {
            "culvert": (c.get("ref") or {}).get("culvert"),
            "feature": (c.get("ref") or {}).get("feature"),
            "name": _data((c.get("ref") or {}).get("name")),
            "sentence": c.get("sentence"),
        }
        for c in block.get("not_placed") or []
        if isinstance(c, dict)
    ]
    return {
        "count": block.get("count") or 0,
        "flagged": block.get("flagged") or 0,
        "openings_offered": block.get("openings_offered") or 0,
        "placed": placed,
        "not_placed": not_placed,
    }


def _version(version, full=True, definitions=None, shapes_of=()) -> dict | None:
    """A version as an agent reads it: no shapes; facts as values (definitions once, in ``definitions``)."""
    if not isinstance(version, dict):
        return None
    definitions = {} if definitions is None else definitions
    sizes = version.get("sizes") or {}
    how = version.get("how_created") or {}
    out = {
        "number": version.get("number"),
        "status": version.get("status"),
        "based_on": version.get("based_on"),
        "how_created": {
            "method": how.get("by"),  # target or threshold ("by" would read as a person's text)
            **{k: how.get(k) for k in ("target_count", "threshold_m2", "min_area_m2") if how.get(k) is not None},
            "forced_points": len(how.get("forced_points") or []),
        },
        "counts": {"sub_catchments": sizes.get("count"), "nodes": sizes.get("node_count")},
        "sizes_m2": {k: sizes.get(k) for k in ("smallest_m2", "median_m2", "largest_m2") if k in sizes},
        "confirmed_by": _data(version.get("confirmed_by")),
        "confirmed_at": version.get("confirmed_at"),
        "line_changed_since": bool(version.get("line_changed_since")),
        "culverts_changed_since": bool(version.get("culverts_changed_since")),
        "culverts": _culverts(version),
        "sentences": _sentences(version),
    }
    if not full or "sub_catchments" not in version:
        return out
    out["nodes"] = [
        {
            **{k: n.get(k) for k in ("id", "kind", "lon", "lat", "level_m", "line_catchment_id")},
            "name": _data(n.get("name")),
            **({"culvert": _culvert_of(n)} if _culvert_of(n) else {}),
            "total_catchment": _compact_facts(n.get("total_catchment"), definitions),
        }
        for n in version.get("nodes") or []
    ]
    out["sub_catchments"] = [
        {
            **{k: s.get(k) for k in ("id", "label", "drains_to", "drains_to_outside", "line_catchment_id", "area_ha")},
            "facts": _compact_facts(s.get("facts"), definitions),
            "notes": [_note(n) for n in s.get("notes") or []],
            **({"outline": _rounded(s.get("polygon"))} if s.get("id") in shapes_of else {}),
        }
        for s in version.get("sub_catchments") or []
    ]
    out["links"] = [
        {
            **{k: link.get(k) for k in ("id", "kind", "from_node", "to_node", "from_outside", "to_outside")},
            "facts": _compact_facts(link.get("facts"), definitions),
        }
        for link in version.get("links") or []
    ]
    out["drags"] = [
        {k: d.get(k) for k in ("drag_id", "state", "state_label", "barrier_id")} for d in version.get("drags") or []
    ]
    out["proposals"] = [_proposal(p) for p in version.get("proposals") or []]
    # the edits made by hand still standing, in order (the map's edits line, History and Undo last): undo takes one back
    out["hand_edits"] = [
        {k: e.get(k) for k in ("seq", "kind", "label", "made_by_agent", "drag_id", "state") if k in e}
        for e in version.get("hand_edits") or []
        if isinstance(e, dict)
    ]
    return out


def _job(job) -> dict | None:
    if not isinstance(job, dict):
        return None
    return {k: job.get(k) for k in ("state", "runs_on", "sentence", "saved_version")}


def _set(doc: dict, full=True, shapes_of=()) -> dict:
    """One set of sub-catchments as an agent reads it (with the outlines of the sub-catchments ``shapes_of`` only)."""
    definitions: dict = {}
    out = {
        "sub_catchments_id": doc.get("id"),
        "delineation_id": doc.get("delineation_id"),
        "line_name": _data(doc.get("line_name")),
        "name": _data(doc.get("name")),
        "catchment_ids": doc.get("catchment_ids"),
        "created_by": _data(doc.get("created_by")),
        "versions": [
            {k: v.get(k) for k in ("number", "status", "based_on", "line_changed_since")}
            for v in doc.get("versions") or []
            if isinstance(v, dict)
        ],
        "edits": doc.get("edits"),
        "version": _version(doc.get("version"), full, definitions, set(shapes_of or ())),
        "job": _job(doc.get("job")),
    }
    if full:
        out["fact_definitions"] = definitions
        out["omitted"] = SHAPES_STAY
    return out


def _line_words(doc: dict) -> str:
    name = doc.get("line_name")
    return _data(str(name)) if name else f"delineation {doc.get('delineation_id')}"


def _refused(body, status: int, what: str) -> dict:
    reply = body if isinstance(body, dict) else {}
    sentence = reply.get("detail") or reply.get("error") or reply.get("error_code") or f"HTTP {status}"
    fix = reply.get("fix_sentence")
    out = {
        "outcome": "refused",
        "http_status": status,
        "error_code": reply.get("error_code"),
        "detail": _data(str(sentence)),
        # a sentence that says 'Refused' itself is not prefixed with it again
        "say_to_user": (
            f"{what[:1].upper() + what[1:] if str(sentence).startswith('Refused') else 'Refused ' + what}: "
            f"{_data(str(sentence))}{' ' + fix if fix else ''} Nothing changed."
        ),
    }
    for key in ("drag_ids", "cells", "limit"):
        if key in reply:
            out[key] = reply[key]
    if reply.get("error_code") in ("HIGH_RES_ONLY", "NOT_RUN"):
        out["next_step"] = (
            "Run the line at High resolution (delineate_catchments with mode 'full'), wait with get_delineation, then "
            "create the sub-catchments."
        )
    return out


async def _post(path: str, payload: dict):
    body, status = await client.post(path, json=payload, raise_for_status=False)
    if status >= 500:
        raise ToolError(f"Hydrata API returned {status} for POST {path}: {str(body)[:300]}")
    return body, status


def _links(delineation_id) -> dict:
    return {"panel": "catchments", "line": delineation_id}


# ---------------------------------------------------------------------------
# Tool 1: create_sub_catchments
# ---------------------------------------------------------------------------
@_tool("create_sub_catchments")
async def create_sub_catchments(
    project_id: Annotated[int, "The project ID (Editor rights)"],
    delineation_id: Annotated[int, "`delineation_id` of a line run at High resolution (read it with get_delineation)"],
    sub_catchments_id: Annotated[
        int | None,
        "To create a set again (its draft, or a new draft after a confirmed version): its `sub_catchments_id`. Omit it "
        "to create a new set. An agent creates again only a set an agent made.",
    ] = None,
    target_count: Annotated[
        int | None, "About how many sub-catchments to make (1 to 500). The default is about 20. Not with threshold_m2."
    ] = None,
    threshold_m2: Annotated[
        float | None,
        "Instead of a target number: the stream threshold in square metres (a stream starts where this much ground "
        "drains to it). Not with target_count.",
    ] = None,
    catchment_ids: Annotated[
        list[int] | None,
        "The line's catchments to cover (get_delineation's catchment ids). Default: all of them (or the set's own).",
    ] = None,
    forced_points: Annotated[
        list[list[float]] | None,
        "Places that must be a node, as [[lon, lat], ...] (WGS84), for example a gauge or a road crossing.",
    ] = None,
    name: Annotated[str, "A short name for a new set (up to 200 characters)"] = "",
) -> str:
    """Create sub-catchments from a line's catchments: each sub-catchment drains to a node, with its measured facts (area,
    slope, flow length...), the nodes' total catchments and the reaches between nodes.

    The line must have a High resolution result: a fast-screen result is refused with the sentence 'High res only' (run
    the line at High resolution first). Creating is by a target number (about 20 by default) or a stream threshold, not
    both. On a terrain too large for the web server it is done on cloud compute: the answer is `creating` and
    get_sub_catchments waits for it. The result is a draft: tell the user a person confirms it on the map (you cannot),
    and give them the link (`ui_url`). Shapes stay on the map.
    """
    if target_count is not None and threshold_m2 is not None:
        raise ToolError("Give target_count or threshold_m2, not both. Nothing was sent.")
    payload: dict = {}
    if sub_catchments_id is None:
        payload["delineation_id"] = delineation_id
    if target_count is not None:
        payload["target_count"] = target_count
    if threshold_m2 is not None:
        payload["threshold_m2"] = float(threshold_m2)
    if catchment_ids is not None:
        payload["catchment_ids"] = list(catchment_ids)
    if forced_points:
        payload["forced_points"] = [
            {"lon": lon, "lat": lat} for lon, lat in _lon_lat_pairs(forced_points, "forced_points", 1)
        ]
    if name and sub_catchments_id is None:
        payload["name"] = name
    again = sub_catchments_id is not None
    path = _path(project_id, sub_catchments_id, "create/") if again else _path(project_id)
    body, status = await _post(path, payload)
    links = _links(delineation_id)
    if status >= 400:
        result = _refused(body, status, "creating the sub-catchments")
        say = result.pop("say_to_user")
        await _hand_back(result, project_id, {"http_status": status, "error_code": result["error_code"]}, say, **links)
        return json.dumps(result, indent=2)
    doc = body if isinstance(body, dict) else {}
    result = {**_set(doc, full=False)}
    version = result.get("version") or {}
    job = result.get("job") or {}
    line = _line_words(doc)
    if status == 202 or (version.get("number") is None and job.get("state") == "running"):
        result["outcome"] = "creating"
        result["next_step"] = (
            f"Wait with get_sub_catchments(project_id={project_id}, sub_catchments_id={doc.get('id')}); a timed_out "
            "read is normal: read again later."
        )
        say = (
            f"The sub-catchments of {line} are being created on cloud compute (set {doc.get('id')}). "
            f"{_data(job.get('sentence')) or ''}".rstrip()
        )
    else:
        result["outcome"] = "created_again" if again else "created"
        counts = version.get("counts") or {}
        say = (
            f"I created {_plural(counts.get('sub_catchments') or 0, 'sub-catchment')} "
            f"({_plural(counts.get('nodes') or 0, 'node')}) from {line} (set {doc.get('id')}, version "
            f"{version.get('number')}, a draft). Open the link to see them; a person confirms them on the map."
        )
    result["http_status"] = status
    result["rule"] = CONFIRM_RULE
    summary = {"sub_catchments_id": doc.get("id"), "version": version.get("number"), **(version.get("counts") or {})}
    await _hand_back(result, project_id, summary, say, **links)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 2: get_sub_catchments
# ---------------------------------------------------------------------------
@_tool("get_sub_catchments")
async def get_sub_catchments(
    project_id: Annotated[int, "The project ID"],
    sub_catchments_id: Annotated[int | None, "One set to read: its `sub_catchments_id`"] = None,
    delineation_id: Annotated[int | None, "Without sub_catchments_id: list the sets of this line"] = None,
    version: Annotated[
        int | None, "With sub_catchments_id: read this version number (default: the draft, else the latest)"
    ] = None,
    timeout_seconds: Annotated[
        int,
        "With sub_catchments_id: how long to wait while they are created on cloud compute before returning `timed_out` "
        "(default 110, ceiling 110). 0 = one read.",
    ] = POLL_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
    outlines_of: Annotated[
        list[int] | None,
        "With sub_catchments_id: the sub-catchments whose outlines to include (GeoJSON in lon/lat, as `outline`), for "
        "example the two either side of a boundary you want to propose moving. At most 10.",
    ] = None,
    streams_of: Annotated[
        list[int] | None,
        "With sub_catchments_id: the sub-catchments whose streams to include (GeoJSON MultiLineString in lon/lat, as "
        "`streams`): the streams a divide goes on, as the map draws them. Every sub-catchment then says `has_stream` "
        "(false: no stream to divide on; add a forced point instead). At most 10.",
    ] = None,
) -> str:
    """Read sub-catchments. Without sub_catchments_id: the project's sets (of one line, with delineation_id), each with its
    versions. With it: one set, waiting while it is created on cloud compute: its nodes (each with its total catchment's
    facts), sub-catchments (id, label, the node it drains to, area, facts and its 'can share water' notes with their ids),
    reaches and links with their facts, the dragged boundaries and their state (waiting, in the terrain, kept as drawn),
    the proposals waiting for a person, and `sentences` to relay (for example 'The line has run again since these were
    created.'). Each fact's unit, definition and source is given once, in `fact_definitions`. Shapes stay on the map,
    except the outlines you ask for with outlines_of, and the streams a divide goes on with streams_of (aim a divide at
    one). `timed_out` is normal: read again later. Read-only.
    """
    wanted = list(outlines_of or [])
    if len(wanted) > MAX_OUTLINES:
        raise ToolError(f"outlines_of takes at most {MAX_OUTLINES} sub-catchments. Nothing was read.")
    streams_wanted = list(streams_of or [])
    if len(streams_wanted) > MAX_OUTLINES:
        raise ToolError(f"streams_of takes at most {MAX_OUTLINES} sub-catchments. Nothing was read.")
    if sub_catchments_id is None:
        params = {"delineation_id": delineation_id} if delineation_id is not None else None
        listed = await client.get(_path(project_id), params=params)
        rows = (listed or {}).get("results") or [] if isinstance(listed, dict) else []
        result = {"count": len(rows), "results": [_set(r, full=False) for r in rows if isinstance(r, dict)]}
        return json.dumps(result, indent=2)

    async def fetch():
        params = {"version": version} if version is not None else None
        return await client.get(_path(project_id, sub_catchments_id), params=params)

    def creating(doc) -> bool:
        return isinstance(doc, dict) and ((doc.get("job") or {}).get("state") == "running") and not doc.get("version")

    doc, polls, elapsed, timed_out = await _poll_until(
        fetch, lambda d: not creating(d), timeout_seconds, poll_interval_seconds
    )
    doc = doc if isinstance(doc, dict) else {}
    result = _set(doc, shapes_of=wanted)
    job = result.get("job") or {}
    if timed_out:
        outcome = "timed_out"
    elif job.get("state") == "failed" and not result.get("version"):
        outcome = "failed"
    else:
        outcome = "ready"
    shown = result.get("version") or {}
    if streams_wanted and shown.get("number") is not None:
        result["streams"] = await _streams(project_id, sub_catchments_id, shown, streams_wanted)
    say = " ".join(shown.get("sentences") or [])
    if outcome == "failed":
        say = _data(job.get("sentence")) or "The sub-catchments could not be created."
    elif outcome == "timed_out":
        say = "Still being created on cloud compute. Read again later."
    result = {"outcome": outcome, "polls": polls, "elapsed_seconds": elapsed, **result, "rule": CONFIRM_RULE}
    if say:
        result["say_to_user"] = say
    return json.dumps(result, indent=2)


async def _streams(project_id, set_id, version: dict, wanted: list) -> dict:
    """TASK-4575 (W12.9, D16): the streams a divide goes on (the API's streams/ read, what the map draws): every
    sub-catchment of ``version`` gets `has_stream`, those in ``wanted`` their `streams` (rounded); a refusal is relayed."""
    from .client import HydrataAPIError

    try:
        doc = await client.get(_path(project_id, set_id, "streams/"), params={"version": version.get("number")})
    except HydrataAPIError as exc:
        return {"sentence": _data(str(exc))}
    doc = doc if isinstance(doc, dict) else {}
    marks = {s.get("id"): s for s in doc.get("sub_catchments") or [] if isinstance(s, dict)}
    for sub in version.get("sub_catchments") or []:
        mark = marks.get(sub.get("id")) or {}
        sub["has_stream"] = bool(mark.get("has_stream"))
        if sub.get("id") in wanted and mark.get("has_stream"):
            sub["streams"] = _rounded(mark.get("streams"))
    return {k: doc.get(k) for k in ("threshold_m2", "definition", "no_stream_sentence")}


# ---------------------------------------------------------------------------
# Tool 3: edit_sub_catchments
# ---------------------------------------------------------------------------
@_tool("edit_sub_catchments")
async def edit_sub_catchments(
    project_id: Annotated[int, "The project ID (Editor rights)"],
    sub_catchments_id: Annotated[int, "The set to change: its `sub_catchments_id`"],
    kind: Annotated[
        Literal[
            "divide", "merge", "move_node", "add_forced_point", "undo_drag", "undo", "make_link", "take_culverts_in"
        ],
        "divide: a new node on the stream of the sub-catchment at lon/lat (it divides there; aim at a stream "
        "get_sub_catchments' streams_of gives); merge: two sub-catchments "
        "of one node, or one with the one its water runs into (sub_catchment_ids); move_node: node_id to lon/lat; "
        "add_forced_point: a node exactly at lon/lat; undo_drag: take back a dragged boundary (drag_id); undo: take back "
        "one other edit made by hand (seq, from the read's hand_edits), as the map's Undo last and History do; make_link: "
        "PROPOSE that a 'can share water' note (note_id) becomes a link, with a reason: a person accepts it on the map; "
        "take_culverts_in: when the read says 'Culverts have changed since these were created.', place the project's "
        "culverts as they are now (the draft is made again, its edits by hand kept; after a confirm, a new draft).",
    ],
    lon: Annotated[float | None, "For divide, move_node and add_forced_point: longitude (WGS84)"] = None,
    lat: Annotated[float | None, "For divide, move_node and add_forced_point: latitude (WGS84)"] = None,
    sub_catchment_ids: Annotated[list[int] | None, "For merge: the ids of the two sub-catchments"] = None,
    node_id: Annotated[int | None, "For move_node: the node's id"] = None,
    node_kind: Annotated[
        Literal["forced_point", "culvert_inlet"], "For add_forced_point: a forced point (default) or a culvert inlet"
    ] = "forced_point",
    drag_id: Annotated[str | None, "For undo_drag: the dragged boundary's `drag_id`"] = None,
    seq: Annotated[int | None, "For undo: the edit's `seq` (get_sub_catchments, version.hand_edits)"] = None,
    note_id: Annotated[int | None, "For make_link: the note's id (get_sub_catchments, a sub-catchment's notes)"] = None,
    reason: Annotated[
        str, "For make_link: one or two sentences the person reads before accepting (what you saw, and why)."
    ] = "",
) -> str:
    """Change sub-catchments by hand on the draft: divide a sub-catchment, merge two, move a node, add a forced point,
    undo a dragged boundary, or undo another edit made by hand (its seq). An edit after a confirm starts a new draft;
    the confirmed version never changes. The answer names the sub-catchments measured again. Ids change when an edit
    writes the draft again: read the set again with get_sub_catchments before the next edit.

    make_link changes nothing: it is filed as a proposal a person accepts or rejects on the map. Tell the user it is
    waiting and give them the link (`ui_url`). To propose a new boundary, use propose_sub_catchment_boundary.
    """
    label = EDIT_KINDS[kind]
    if kind == "take_culverts_in":
        return await _take_culverts_in(project_id, sub_catchments_id)
    if kind == "make_link":
        if isinstance(note_id, bool) or not isinstance(note_id, int):
            raise ToolError("make_link needs note_id (a sub-catchment's note in get_sub_catchments). Nothing was sent.")
        payload = {"kind": "make_link", "note_id": note_id, "note": _reason(reason)}
        return await _propose_link(project_id, sub_catchments_id, payload)
    payload: dict = {"kind": kind}
    if kind == "merge":
        ids = list(sub_catchment_ids or [])
        if len(ids) != 2 or ids[0] == ids[1]:
            raise ToolError("merge needs sub_catchment_ids: two different sub-catchments. Nothing was sent.")
        payload["sub_catchment_ids"] = ids
    elif kind == "undo":
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ToolError("undo needs seq (get_sub_catchments, version.hand_edits). Nothing was sent.")
        payload["seq"] = seq
    elif kind == "undo_drag":
        if not isinstance(drag_id, str) or not drag_id.strip():
            raise ToolError("undo_drag needs drag_id (get_sub_catchments, drags). Nothing was sent.")
        payload["drag_id"] = drag_id.strip()
    else:
        if kind == "move_node":
            if isinstance(node_id, bool) or not isinstance(node_id, int):
                raise ToolError("move_node needs node_id. Nothing was sent.")
            payload["node_id"] = node_id
        [[x, y]] = _lon_lat_pairs([[lon, lat]] if lon is not None and lat is not None else None, "lon/lat", 1)
        payload.update(lon=x, lat=y)
        if kind == "add_forced_point":
            payload["node_kind"] = node_kind
    path = _path(project_id, sub_catchments_id, "edits/")
    body, status = await _post(path, payload)
    doc = body if isinstance(body, dict) else {}
    if status >= 400:
        result = _refused(body, status, label.lower())
        say = result.pop("say_to_user")
        await _hand_back(result, project_id, {"http_status": status, "error_code": result["error_code"]}, say)
        return json.dumps(result, indent=2)
    edit = doc.get("edit") or {}
    result = {
        "outcome": "changed",
        "http_status": status,
        "edit": {k: edit.get(k) for k in ("seq", "kind", "label")},
        "remeasured": doc.get("remeasured") or [],
        **_set(doc),
        "rule": CONFIRM_RULE,
    }
    version = result.get("version") or {}
    say = (
        f"{edit.get('label') or label}: done on the draft of the sub-catchments of {_line_words(doc)} (version "
        f"{version.get('number')}); {_plural(len(result['remeasured']), 'sub-catchment')} measured again."
    )
    summary = {"sub_catchments_id": doc.get("id"), "edit": edit.get("seq"), "remeasured": result["remeasured"]}
    await _hand_back(result, project_id, summary, say, **_links(doc.get("delineation_id")))
    return json.dumps(result, indent=2)


async def _take_culverts_in(project_id, sub_catchments_id) -> str:
    """POST culverts/: the draft made again by the ladder with the project's culverts as they are now."""
    body, status = await _post(_path(project_id, sub_catchments_id, "culverts/"), {})
    doc = body if isinstance(body, dict) else {}
    if status >= 400:
        result = _refused(body, status, "taking the culverts in")
        say = result.pop("say_to_user")
        await _hand_back(result, project_id, {"http_status": status, "error_code": result["error_code"]}, say)
        return json.dumps(result, indent=2)
    summary = doc.get("culverts_taken_in") or {}
    result = {
        "outcome": "changed" if doc.get("changed") else "unchanged",
        "http_status": status,
        "culverts_taken_in": {
            "added": [_data(n) for n in summary.get("added") or []],
            "removed": [_data(n) for n in summary.get("removed") or []],
            "moved": [_data(n) for n in summary.get("moved") or []],
            "placed": summary.get("placed"),
            "flagged": summary.get("flagged"),
        },
        **_set(doc),
        "rule": CONFIRM_RULE,
    }
    version = result.get("version") or {}
    if doc.get("changed"):
        parts = [f"{summary.get('placed') or 0} placed"]
        detail = [f"{len(summary.get(k) or [])} {k}" for k in ("added", "moved", "removed") if summary.get(k)]
        if detail:
            parts[0] += f" ({', '.join(detail)})"
        parts.append(f"{summary.get('flagged') or 0} flagged")
        say = (
            f"Took the culverts in: {', '.join(parts)}, on the draft of the sub-catchments of {_line_words(doc)} "
            f"(version {version.get('number')})."
        )
    else:
        say = "The culverts are as these sub-catchments already have them: nothing changed."
    extra = " ".join(version.get("sentences") or [])
    if extra:
        say += f" {extra}"
    await _hand_back(
        result,
        project_id,
        {"sub_catchments_id": doc.get("id"), "culverts_taken_in": summary},
        say,
        **_links(doc.get("delineation_id")),
    )
    return json.dumps(result, indent=2)


async def _propose_link(project_id, sub_catchments_id, payload: dict) -> str:
    path = _path(project_id, sub_catchments_id, "proposals/")
    body, status = await _post(path, payload)
    reply = body if isinstance(body, dict) else {}
    # the line to open: the set's own line (best effort; the proposal is filed either way)
    line_id = None
    try:
        shown = await client.get(_path(project_id, sub_catchments_id))
        line_id = shown.get("delineation_id") if isinstance(shown, dict) else None
    except Exception:  # the link is a convenience; never hide the answer behind it
        line_id = None
    if status >= 400:
        result = _refused(body, status, "the proposal")
        say = result.pop("say_to_user")
        await _hand_back(
            result, project_id, {"http_status": status, "error_code": result["error_code"]}, say, **_links(line_id)
        )
        return json.dumps(result, indent=2)
    proposal = _proposal(reply)
    waiting = reply.get("status") == "proposed"
    result = {
        "outcome": "proposed" if waiting else "stored",
        "status": "waiting" if waiting else reply.get("status"),
        "http_status": status,
        "proposal": proposal,
        "sentence": reply.get("sentence"),
        "rule": PROPOSAL_RULE,
    }
    rise = proposal.get("rise_m")
    say = (
        f"I proposed making a 'can share water' note a link (proposal {proposal.get('id')}"
        f"{f', a rise of {rise:g} m' if isinstance(rise, (int, float)) else ''}): {proposal['note']} "
        "Open the link and accept or reject it on the map. Nothing changes until you accept it."
    )
    summary = {"proposal_id": proposal.get("id"), "kind": "make_link", "status": result["status"]}
    await _hand_back(result, project_id, summary, say, **_links(line_id))
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 4: propose_sub_catchment_boundary
# ---------------------------------------------------------------------------
@_tool("propose_sub_catchment_boundary")
async def propose_sub_catchment_boundary(
    project_id: Annotated[int, "The project ID (Editor rights)"],
    sub_catchments_id: Annotated[int, "The set: its `sub_catchments_id`"],
    sub_catchment_ids: Annotated[list[int], "The two sub-catchments either side of the boundary (their ids)"],
    line: Annotated[
        list[list[float]],
        "The new stretch of boundary as [[lon, lat], ...] (at least 2 points, WGS84). Start and end it ON the boundary "
        "the two share now; the ground between the old and the new boundary changes sides.",
    ],
    reason: Annotated[
        str,
        "One or two sentences the person reads before deciding: what you saw (for example a road or a drain the terrain "
        "does not hold) and why the boundary goes this way.",
    ],
    preview_terrain: Annotated[
        bool,
        "True: also work out (without storing anything) how much of the dragged ground the terrain would send the same "
        "way with a drainage barrier along the line, and say so. It takes a little longer.",
    ] = False,
) -> str:
    """Propose a new boundary between two sub-catchments (a dragged boundary). It is filed WAITING: the draft shows it, and
    only a person decides it on the map (Keep as drawn, or Make the terrain agree, which stores a drainage barrier), or
    undoes it. The sub-catchments cannot be confirmed while it waits. Read the two outlines first (get_sub_catchments
    with outlines_of) and the ground (terrain_profile), and use it only when you can say why water goes this way and the
    terrain does not show it. Tell the user it is waiting and give them the link (`ui_url`).
    """
    ids = list(sub_catchment_ids or [])
    if len(ids) != 2 or ids[0] == ids[1]:
        raise ToolError("sub_catchment_ids must be two different sub-catchments. Nothing was sent.")
    why = _reason(reason)
    labels = {}
    try:  # the labels a person sees on the map, read before the edit writes the draft's rows (and their ids) again
        before = await client.get(_path(project_id, sub_catchments_id))
        rows = ((before or {}).get("version") or {}).get("sub_catchments") or [] if isinstance(before, dict) else []
        labels = {s.get("id"): s.get("label") for s in rows if isinstance(s, dict)}
    except Exception:  # the labels only word the answer; the proposal is filed either way
        labels = {}
    payload = {
        "kind": "drag",
        "sub_catchment_ids": ids,
        "line": {"type": "LineString", "coordinates": _lon_lat_pairs(line, "line", 2)},
        "state": "waiting",
    }
    path = _path(project_id, sub_catchments_id, "edits/")
    body, status = await _post(path, payload)
    doc = body if isinstance(body, dict) else {}
    if status >= 400:
        result = _refused(body, status, "the dragged boundary")
        say = result.pop("say_to_user")
        await _hand_back(result, project_id, {"http_status": status, "error_code": result["error_code"]}, say)
        return json.dumps(result, indent=2)
    edit = doc.get("edit") or {}
    inputs = edit.get("inputs") or {}
    drag_id = inputs.get("drag_id")
    moved = inputs.get("moved_m2")
    shown = _set(doc)
    state = next(
        (d.get("state") for d in (shown.get("version") or {}).get("drags") or [] if d.get("drag_id") == drag_id),
        inputs.get("state"),
    )
    result = {
        "outcome": "proposed",
        "status": state,
        "http_status": status,
        "drag_id": drag_id,
        "moved_ha": round(moved / 10_000, 4) if isinstance(moved, (int, float)) else None,
        "reason": _data(why),
        **shown,
        "rule": DRAG_RULE,
    }
    preview_words = ""
    if preview_terrain and drag_id:
        seen, seen_status = await _post(
            _path(project_id, sub_catchments_id, f"drags/{drag_id}/terrain/"), {"preview": True}
        )
        seen = seen if isinstance(seen, dict) else {}
        if seen_status < 400:
            difference = seen.get("difference") or {}
            result["terrain_preview"] = {
                "sentence": seen.get("sentence"),
                "offer_sentence": seen.get("offer_sentence"),
                **{k: difference.get(k) for k in ("agree_pct", "differ_ha", "max_distance_m", "larger_than_dragged")},
            }
            preview_words = " ".join(s for s in (seen.get("sentence"), seen.get("offer_sentence")) if s)
        else:
            refused = _refused(seen, seen_status, "the terrain preview")
            result["terrain_preview"] = {k: refused.get(k) for k in ("outcome", "http_status", "error_code", "detail")}
            preview_words = f"The terrain preview could not be worked out: {refused['detail']}"
    names = " and ".join(str(labels[i]) if labels.get(i) is not None else f"id {i}" for i in ids)
    say = (
        f"I proposed a new boundary between sub-catchments {names} of {_line_words(doc)} (dragged boundary {drag_id}): "
        f"{_data(why)} It is waiting: open the link and choose Keep as drawn or Make the terrain agree, or undo it. "
        "The sub-catchments cannot be confirmed while it waits."
    )
    if preview_words:
        say += f" {preview_words}"
    summary = {"sub_catchments_id": doc.get("id"), "drag_id": drag_id, "status": state, "moved_m2": moved}
    await _hand_back(result, project_id, summary, say, **_links(doc.get("delineation_id")))
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 5: propose_culvert_openings (W11.5, TASK-4487)
# ---------------------------------------------------------------------------
def _culvert_picks(culverts) -> list | None:
    if culverts is None:
        return None
    if (
        not isinstance(culverts, list)
        or not culverts
        or any(
            not isinstance(p, (list, tuple))
            or len(p) != 2
            or isinstance(p[0], bool)
            or not isinstance(p[0], int)
            or isinstance(p[1], bool)
            for p in culverts
        )
    ):
        raise ToolError(
            "culverts must be [[culvert, feature], ...] pairs from get_sub_catchments (version.culverts.placed), or "
            "omitted for every opening offered. Nothing was sent."
        )
    return [[p[0], p[1]] for p in culverts]


@_tool("propose_culvert_openings")
async def propose_culvert_openings(
    project_id: Annotated[int, "The project ID (Editor rights)"],
    sub_catchments_id: Annotated[int, "The set of sub-catchments: its `sub_catchments_id`"],
    culverts: Annotated[
        list[list[int | str]] | None,
        "Only these culverts' openings, as [[culvert, feature], ...] (get_sub_catchments: version.culverts.placed, "
        "where opening_offered is true). Omit it for every opening offered, which is the usual one action.",
    ] = None,
) -> str:
    """Propose the drainage opening every flagged culvert of the sub-catchments offers, in ONE request (D15's measured
    ladder: where the water reaching a culvert's inlet leaves past its outlet, an opening along the culvert would carry it
    through). The openings are filed as PROPOSALS: nothing changes until a person accepts them on the map, and only then
    is the terrain's drainage analysis worked out again (once) and the line run again. Read the culverts first with
    get_sub_catchments (version.culverts: each culvert's area across the inlet, its flags and whether an opening is
    offered). Tell the user they are waiting and give them the link (`ui_url`).
    """
    picks = _culvert_picks(culverts)
    payload = {} if picks is None else {"culverts": picks}
    body, status = await _post(_path(project_id, sub_catchments_id, "culvert-openings/"), payload)
    reply = body if isinstance(body, dict) else {}
    line_id = None
    try:  # the line to open (best effort; the proposals are filed either way)
        shown = await client.get(_path(project_id, sub_catchments_id))
        line_id = shown.get("delineation_id") if isinstance(shown, dict) else None
    except Exception:  # the link is a convenience; never hide the answer behind it
        line_id = None
    links = _links(line_id)
    if status >= 400:
        result = _refused(body, status, "the culvert openings")
        say = result.pop("say_to_user")
        await _hand_back(result, project_id, {"http_status": status, "error_code": result["error_code"]}, say, **links)
        return json.dumps(result, indent=2)
    openings = [
        {
            **{k: o.get(k) for k in ("id", "kind", "label", "status", "proposed_by_agent", "waiting_sentence")},
            "note": _data(o.get("note")),
        }
        for o in reply.get("openings") or []
        if isinstance(o, dict)
    ]
    n = reply.get("stored") or 0
    there = reply.get("already_there") or 0
    by_agent = bool(reply.get("proposed"))
    if n:
        outcome = "proposed" if by_agent else "stored"
    else:
        outcome = "already_there"
    result = {
        "outcome": outcome,
        "status": "waiting" if by_agent and n else outcome,
        "http_status": status,
        "proposed": n if by_agent else 0,
        "already_there": there,
        "openings": openings,
        "drainage_analysis_job": reply.get("drainage_analysis_job"),
        "sentence": reply.get("sentence"),
        "rule": OPENINGS_RULE,
    }
    if n and by_agent:
        say = (
            f"{reply.get('sentence') or _plural(n, 'drainage opening') + ' proposed.'} Nothing changes until a person "
            f"accepts {'it' if n == 1 else 'them'} on the map: open the link to see "
            f"{'it' if n == 1 else 'them'} with the line's drainage corrections."
        )
    elif n:
        say = reply.get("sentence") or f"{_plural(n, 'drainage opening')} stored with the terrain."
    else:
        say = (
            f"{'The opening is' if there == 1 else 'The openings are'} already on the terrain (proposed or stored): "
            "nothing new was filed."
        )
    if there and n:
        say += f" {there} {'was' if there == 1 else 'were'} already on the terrain."
    summary = {"sub_catchments_id": sub_catchments_id, "proposed": result["proposed"], "already_there": there}
    await _hand_back(result, project_id, summary, say, **links)
    return json.dumps(result, indent=2)
