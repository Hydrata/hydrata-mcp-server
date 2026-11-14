"""TASK-4404 (epic 4274 W4.1) - catchments from a delineation line, through three tools.

delineate_catchments  - a line on the project's terrain makes a delineation and starts its job (returns at once, with
                        the estimate); with a delineation id it changes the two areas, the name or the start chainage
                        of the agent's own unconfirmed delineation (a regroup, no new job) or starts its full run.
get_delineation       - reads one, waiting for its job the way get_terrain_merge waits for a merge.
use_catchments_as_boundary - a Closed (Reflective) model boundary round chosen catchments, with a short Transmissive
                        opening at each drainage point (decisions D14 = A, D15 = A).

Every result carries the API's own field names (TASK-4397, 4398, 4410, 4417) and, beside each chainage written
CH k+mmm, its distance in metres. Every warning comes back as its sentence. The tools never tick, apply or undo a
diversion and never confirm: only a person does, in the browser (decision D13). What an agent makes is unreviewed
until a person confirms it. There is no tool that deletes a delineation or edits its line.
"""

import json
from typing import Annotated, Literal

from fastmcp.exceptions import ToolError

from ._common import _data, _tool, client
from ._helpers import (
    POLL_DEFAULT_TIMEOUT_SECONDS,
    SOURCES_ARGUMENT_HELP,
    TOKEN_REFRESH_NEXT_STEP,
    _clean_sources,
    _hand_back,
    _poll_until,
    _token_cap_spent,
)
from .terrain import _lon_lat_pairs

LIVE_STATUSES = ("pending", "running")
# What stays on the screen: the drawing data. The agent reads the ground itself with terrain_profile.
OMITTED = (
    "The drawing data (the ground profile along the line, each catchment's polygon, the line's own vertices) is left "
    "on the map; use terrain_profile to read the ground along a line."
)
SUGGESTIONS_NOTE = (
    "Diversion suggestions are information only: a drain that could send one catchment's water to its neighbour. "
    "Only a person ticks one, in the browser; no tool applies, ticks or undoes a diversion."
)
BOUNDARY_RULE = (
    "The boundary is Closed (Reflective) everywhere except a short open stretch, typed Transmissive, at each included "
    "drainage point (5 working cells long, never under 20 m). The delineation line itself is not open."
)
PERSON_ONLY = "Only a person in the browser can tick a diversion, check one again, or confirm a delineation."


# ---------------------------------------------------------------------------
# Words and numbers
# ---------------------------------------------------------------------------
def _chainage_text(metres) -> str | None:
    """5081 m -> 'CH 5+081' (the API's own writing: a negative chainage keeps its sign before the kilometres)."""
    if isinstance(metres, bool) or not isinstance(metres, (int, float)):
        return None
    whole = int(round(metres))
    sign = "-" if whole < 0 else ""
    whole = abs(whole)
    return f"CH {sign}{whole // 1000}+{whole % 1000:03d}"


def _m(value) -> str:
    """A length in metres for a sentence: at most one decimal ('28.1')."""
    return f"{round(value, 1):g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "unknown"


def _duration(seconds) -> str:
    """Seconds as '2 h 1 min' / '45 min' / '30 s'."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        return "an unknown time"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def _with_text(item: dict, *pairs) -> dict:
    """`item` plus a 'CH k+mmm' text beside each `<x>chainage_m` (the distance is already beside it)."""
    out = dict(item)
    for metres_key, text_key in pairs:
        if metres_key in item:
            out[text_key] = _chainage_text(item[metres_key])
    return out


def _catchment_doc(c: dict) -> dict:
    out = _with_text(
        c,
        ("chainage_m", "chainage"),
        ("from_chainage_m", "from_chainage"),
        ("to_chainage_m", "to_chainage"),
    )
    if isinstance(c.get("arrival_point"), dict):
        out["arrival_point"] = _with_text(c["arrival_point"], ("chainage_m", "chainage"))
    if c.get("area_is_lower_bound"):
        out["area_note"] = (
            f"Its area is at least {_m(c.get('area_ha'))} ha: the terrain ends inside this catchment, so its true area is "
            "larger. Add terrain beyond the edge for the full area."
        )
    return out


def _names(doc: dict) -> dict:
    return {c.get("id"): c.get("name") for c in doc.get("catchments") or [] if isinstance(c, dict)}


def _shares_sentence(item: dict, names: dict) -> str:
    """The API's shares-water wording (TASK-4405): where it applies first, the measured height, then the plain consequence."""
    a, b = (item.get("catchments") or [None, None])[:2]
    pair = f"{_data(str(names.get(a, a)))} and {_data(str(names.get(b, b)))}"
    height = item.get("pass_height_m")
    measured = isinstance(height, (int, float)) and not isinstance(height, bool)
    if item.get("where") == "upstream":
        lead = f"{pair} can share water upstream of the line"
        how = f"; the ground between them is {height:.2f} m above the channel" if measured else ""
    else:
        at = _chainage_text(item.get("pass_chainage_m"))
        lead = f"{f'{at} (high point): ' if at else ''}{pair} can share water"
        how = f"; water ponds {height:.2f} m before it spills from one into the other" if measured else ""
    return f"{lead}{how}. Both areas are uncertain."


def _meets_twice_sentence(item: dict, names: dict) -> str:
    """The API's meets-twice wording (TASK-4405): the catchment first, where its water reaches the line again, what follows."""
    here = _data(str(names.get(item.get("catchment"), item.get("catchment"))))
    again = _data(str(names.get(item.get("meets_again_in"), item.get("meets_again_in"))))
    return (
        f"{here}: water leaving its drainage point reaches the line again at {_chainage_text(item.get('chainage_m'))}, "
        f"in {again}. The area of {again} does not include the area of {here}."
    )


def _ticks_text(doc: dict) -> str | None:
    to_check = doc.get("diversions_to_check") or 0
    remembered = [d for d in doc.get("remembered_diversions") or [] if d.get("needs_check")]
    if not to_check and not remembered:
        return None
    parts = []
    if to_check:
        parts.append(
            f"{to_check} diversion{' a person ticked was' if to_check == 1 else 's a person ticked were'} re-applied "
            "after the catchments changed and must be checked again"
        )
    if remembered:
        parts.append(
            f"{len(remembered)} more tick{' is' if len(remembered) == 1 else 's are'} remembered but not applied now "
            "(the catchments it joins are grouped away by these settings)"
        )
    return (
        "; ".join(parts)
        + f". {PERSON_ONLY} Tell the user to open the link and check them; the delineation cannot be confirmed until then."
    )


def _full_run_doc(job: dict) -> dict:
    keys = (
        "status",
        "mode",
        "phase",
        "progress",
        "step",
        "step_pct",
        "runs_on",
        "handed_over",
        "started_at",
        "elapsed_s",
        "expected_remaining_s",
        "expected_finish_at",
        "error",
    )
    out = {k: job.get(k) for k in keys if k in job}
    out["phase"] = _phase_of(job)
    return out


def _phase_of(job: dict) -> str:
    status = job.get("status")
    if status in LIVE_STATUSES:
        return job.get("step") or ("queued" if status == "pending" else "running")
    return "error" if status in ("error", "cancelled") else "ready"


def _result(doc: dict) -> dict:
    """The tool result block of a read of one delineation: the API's fields, chainages with their text, sentences."""
    names = _names(doc)
    line = {k: v for k, v in (doc.get("line") or {}).items() if k != "geometry"}
    out: dict = {
        "delineation_id": line.get("id"),
        "line": line,
        "delineation": doc.get("delineation"),
        "shown_result": doc.get("shown_result"),
        "tally": doc.get("tally"),
        "catchments": [_catchment_doc(c) for c in doc.get("catchments") or []],
        "high_points": [_with_text(h, ("chainage_m", "chainage")) for h in doc.get("high_points") or []],
        "suggestions": [
            {**s, "from_name": names.get(s.get("from")), "to_name": names.get(s.get("to"))}
            for s in doc.get("suggestions") or []
        ],
        "suggestions_note": SUGGESTIONS_NOTE,
        "diversions": doc.get("diversions") or [],
        "remembered_diversions": doc.get("remembered_diversions") or [],
        "diversions_to_check": doc.get("diversions_to_check") or 0,
        "shares_water": [
            {**_with_text(s, ("pass_chainage_m", "pass_chainage")), "sentence": _shares_sentence(s, names)}
            for s in doc.get("shares_water") or []
        ],
        "meets_line_twice": [
            {
                **_with_text(m, ("chainage_m", "chainage")),
                "sentence": _meets_twice_sentence(m, names),
            }
            for m in doc.get("meets_line_twice") or []
        ],
        "warnings": [_with_text(w, ("chainage_m", "chainage")) for w in doc.get("warnings") or []],
        "ignored_diversions": doc.get("ignored_diversions") or [],
        "job": doc.get("job"),
        "omitted": OMITTED,
    }
    out["warnings_in_words"] = [w.get("sentence") for w in out["warnings"] if w.get("sentence")]
    return out


def _who(doc: dict) -> str:
    d = doc.get("delineation") or {}
    if d.get("confirmed"):
        return f"A person ({_data(str(d.get('confirmed_by')))}) confirmed it on {d.get('confirmed_at')}."
    if d.get("agent_authored"):
        return (
            "An agent made it and no person has confirmed it, so it is unreviewed: only a person confirms it, in the "
            "browser."
        )
    return f"A person ({_data(str(d.get('created_by')))}) made it; no person has confirmed it yet."


def _read_words(doc: dict, result: dict, ticks: str | None = None) -> list[str]:
    line, tally = doc.get("line") or {}, doc.get("tally") or {}
    shown = doc.get("shown_result") or {}
    parts = []
    if shown:
        mode = shown.get("mode") or "quick"
        coarse = ", coarsened to the web server's limit" if shown.get("coarsened") else ""
        diverted = tally.get("diverted", 0) or 0
        # every suggestion listed; the API's tally.suggested counts only the shallow ones (a drain 2 m deep or less)
        suggested = len(doc.get("suggestions") or [])
        shallow = tally.get("suggested", 0) or 0
        parts.append(
            f"Delineation {line.get('id')} ({_data(str(line.get('name')))}) has {tally.get('catchments')} catchments "
            f"totalling {tally.get('total_area_ha', 0):.1f} ha; {diverted} diversion{'' if diverted == 1 else 's'} applied, "
            f"{suggested} diversion{'' if suggested == 1 else 's'} suggested (information only), {shallow} with a drain "
            f"2 m deep or less. Shown result: {mode}, worked at "
            f"{_m(shown.get('working_resolution_m'))} m on a {_m(shown.get('terrain_resolution_m'))} m terrain{coarse}."
        )
    parts.append(_who(doc))
    if ticks:
        parts.append(ticks)
    words = result["warnings_in_words"]
    if words:
        parts.append("Warnings, relay every one: " + " ".join(f"({i}) {w}" for i, w in enumerate(words, start=1)))
    else:
        parts.append("The API raised no warnings.")
    if result["shares_water"]:
        parts.append(
            f"{len(result['shares_water'])} pair(s) of neighbouring catchments can share water: both areas of each pair are "
            "uncertain."
        )
    replaced = shown.get("replaced") if shown.get("mode") == "full" else None
    if isinstance(replaced, dict):
        lost = (replaced.get("not_carried_over") or {}).get("catchments") or []
        if lost:
            parts.append(
                f"The full-resolution result replaced the quick look; {len(lost)} catchment(s) of the quick look have no "
                "match in it."
            )
        if replaced.get("confirmation_lapsed"):
            parts.append("The new result lapsed an earlier confirmation: a person needs to confirm again.")
    return parts


def _estimate_words(estimate: dict) -> str:
    out = []
    for mode in ("quick", "full"):
        e = estimate.get(mode) or {}
        low = (e.get("expected_seconds") or {}).get("low")
        high = (e.get("expected_seconds") or {}).get("high")
        where = "AWS Batch" if e.get("runs_on") == "batch" else "the web server"
        text = (
            f"{mode}: about {e.get('cells'):,} cells at {_m(e.get('working_resolution_m'))} m on {where}, "
            f"expected {_duration(low)} to {_duration(high)}"
            f"{', it can take hours' if e.get('can_take_hours') else ''}"
        )
        if e.get("may_move_to_batch"):
            text += " (a web-server run that overruns is handed to Batch)"
        text += "."
        if not e.get("can_run"):
            reason = str(e.get("cannot_run_reason") or "").strip()
            text += f" The {mode} run CANNOT run here: {reason}{'' if reason.endswith('.') else '.'}"
        out.append(text)
    note = (estimate.get("quick") or {}).get("note")
    return "Estimate (a range, not a promise). " + " ".join(out) + (f" {note}" if note else "")


def _refused(body, status: int, what: str) -> dict:
    reply = body if isinstance(body, dict) else {"response": body}
    sentence = reply.get("detail") or reply.get("error") or reply.get("error_code") or f"HTTP {status}"
    out = {
        "outcome": "refused",
        "http_status": status,
        "error_code": reply.get("error_code"),
        "hydrata_reply": reply,
        "say_to_user": (
            f"Hydrata refused {what}: {sentence} Nothing was changed. If this is a person's delineation or one a "
            "person confirmed, ask the user to change it in the browser."
        ),
    }
    return out


def _estimate_summary(estimate: dict) -> dict:
    return {
        m: {k: (estimate.get(m) or {}).get(k) for k in ("cells", "runs_on", "can_run", "expected_seconds")}
        for m in ("quick", "full")
    }


# ---------------------------------------------------------------------------
# Tool 1: delineate_catchments
# ---------------------------------------------------------------------------
@_tool("delineate_catchments")
async def delineate_catchments(
    project_id: Annotated[int, "The project ID (Contributor rights)"],
    delineation_id: Annotated[
        int | None,
        "Omit to make a NEW delineation. Give the id of YOUR OWN unconfirmed delineation to change its name, "
        "start_chainage_m, min_area_ha or max_divert_area_ha (the result is regrouped, no new job) or, with "
        "mode='full', to start its full-resolution run. A delineation a person made or confirmed is refused.",
    ] = None,
    line: Annotated[
        list[list[float]] | None,
        "A new delineation's line: 2 or more [longitude, latitude] pairs in WGS84 degrees (longitude FIRST), put on "
        "natural ground where the water arrives (the toe of a slope or in the channel), not on a crest. Read the "
        "ground along a candidate line with terrain_profile first. Not accepted with delineation_id (the line cannot "
        "be moved by a tool).",
    ] = None,
    name: Annotated[str, "A new delineation's name (required); with delineation_id, the new name"] = "",
    start_chainage_m: Annotated[
        float | None, "Chainage of the start of the line in metres (default 0); CH 5+081 is 5081"
    ] = None,
    min_area_ha: Annotated[
        float | None, "Minimum catchment area in hectares: smaller catchments are grouped with a neighbour"
    ] = None,
    max_divert_area_ha: Annotated[
        float | None,
        "Maximum catchment diversion area in hectares: the largest catchment a suggested diversion may send away",
    ] = None,
    terrain_id: Annotated[
        int | None, "A new delineation's terrain (default: the project's newest ready terrain)"
    ] = None,
    mode: Annotated[
        Literal["quick", "full"] | None,
        "'quick' (the default for a new delineation): coarsened to the web server's limit, seconds to a couple of "
        "minutes. 'full': at the terrain's own resolution, on AWS Batch, can take many hours (up to about 24); the "
        "quick result stays usable and the person can close the map. Do the quick look first and offer the full "
        "run with its estimate. With delineation_id, 'full' starts the full run; 'quick' runs it again.",
    ] = None,
    estimate_only: Annotated[
        bool,
        "True runs nothing: it returns the estimate (cells, working resolution, where it runs, expected time range, "
        "and whether it can run on this site) for the terrain, or for the delineation_id's terrain.",
    ] = False,
    authored_method: Annotated[
        str,
        "One short phrase saying how you placed the line and chose the settings (e.g. 'toe of the slope read with "
        "terrain_profile at 5 m'). Shown to the person who reviews it.",
    ] = "",
    sources: Annotated[list[dict] | None, SOURCES_ARGUMENT_HELP] = None,
) -> str:
    """Make catchments from a delineation line on the project's terrain, or change the settings of your own.

    A NEW delineation starts a job and returns AT ONCE with `delineation_id`, `ui_url` (the map on this line's
    Catchments pane) and the `estimate` (cells, resolution, where it runs, expected time range); follow it with
    get_delineation. It never waits on a run. Nothing is confirmed: what you make is unreviewed until a person
    confirms it in the browser. Only a person ticks a diversion or confirms; no tool does, and there is no tool
    that deletes a delineation or moves its line.

    With `delineation_id`: changing the name, start chainage or either area regroups the stored result without
    repeating the terrain step (no new job). A person's diversion ticks survive the change: they are re-applied and
    marked check again until a person checks them; say so. A delineation a person made or confirmed is refused in
    words. `mode='full'` starts the full-resolution run on that delineation.
    """
    clean_sources = _clean_sources(sources)
    method = authored_method.strip() if isinstance(authored_method, str) else ""
    base = f"/projects/{project_id}/delineations/"

    if delineation_id is not None:
        return await _change(
            project_id,
            delineation_id,
            line,
            name,
            start_chainage_m,
            min_area_ha,
            max_divert_area_ha,
            terrain_id,
            mode,
            estimate_only,
            method,
            clean_sources,
        )

    if estimate_only:
        estimate = await client.get(
            f"{base}estimate/", params={"terrain_id": terrain_id} if terrain_id is not None else None
        )
        result = {"outcome": "estimate", "estimate": estimate}
        await _hand_back(
            result,
            project_id,
            {"estimate": _estimate_summary(estimate), "terrain_id": estimate.get("terrain_id")},
            _estimate_words(estimate)
            + " Nothing was run. Offer the quick look first; offer the full run with this estimate.",
            panel="catchments",
        )
        return json.dumps(result, indent=2)

    if not name.strip():
        raise ToolError("name is required for a new delineation (it is the line's name). Nothing was sent.")
    if line is None:
        raise ToolError("line is required for a new delineation: 2 or more [lon, lat] pairs. Nothing was sent.")
    pairs = _lon_lat_pairs(line, "line", 2)
    if len({tuple(p) for p in pairs}) < 2:
        raise ToolError("line has no length: give two different [lon, lat] points. Nothing was sent.")
    chosen = mode or "quick"

    estimate = await client.get(
        f"{base}estimate/", params={"terrain_id": terrain_id} if terrain_id is not None else None
    )
    this = estimate.get(chosen) or {}
    if not this.get("can_run", True):
        result = {"outcome": "cannot_run", "estimate": estimate}
        sentence = this.get("cannot_run_reason") or "This run cannot start on this site."
        await _hand_back(
            result,
            project_id,
            {"mode": chosen, "estimate": _estimate_summary(estimate)},
            f"The {chosen} run cannot start, so no delineation was made: {sentence} "
            + (
                "Do the quick look instead (mode='quick'): " + _estimate_words(estimate)
                if chosen == "full" and (estimate.get("quick") or {}).get("can_run")
                else ""
            ),
            panel="catchments",
        )
        return json.dumps(result, indent=2)

    payload: dict = {"geometry": {"type": "LineString", "coordinates": pairs}, "name": name.strip(), "mode": chosen}
    for key, value in (
        ("start_chainage_m", start_chainage_m),
        ("min_area_ha", min_area_ha),
        ("max_divert_area_ha", max_divert_area_ha),
        ("terrain_id", terrain_id),
    ):
        if value is not None:
            payload[key] = value
    if method:
        payload["authored_method"] = method
    if clean_sources:
        payload["sources"] = clean_sources
    body, status = await client.post(base, json=payload, raise_for_status=False)
    if status >= 500:
        raise ToolError(f"Hydrata API returned {status} for POST {base}: {str(body)[:300]}")
    if status >= 400:
        result = _refused(body, status, "to start the delineation")
        await _hand_back(result, project_id, {"http_status": status}, result.pop("say_to_user"), panel="catchments")
        return json.dumps(result, indent=2)
    new_id = body.get("delineation_id") or (body.get("line") or {}).get("id")
    job = body.get("job") or {}
    result = {
        "outcome": "started",
        "http_status": status,
        "delineation_id": new_id,
        "mode": chosen,
        "line": body.get("line"),
        "delineation": body.get("delineation"),
        "job": job,
        "estimate": estimate,
        "next_step": f"Call get_delineation(project_id={project_id}, delineation_id={new_id}) until it is ready.",
    }
    where = "AWS Batch" if job.get("runs_on") == "batch" else "the web server"
    say = (
        f"The {chosen} delineation '{_data(name.strip())}' (id {new_id}) has started on {where}. "
        "An agent made it, so it is unreviewed: only a person confirms it, in the browser. " + _estimate_words(estimate)
    )
    if chosen == "full":
        say += " A full run can take many hours: the quick result is not made by this call, and the person can close the map."
    else:
        say += " The quick look takes seconds to a couple of minutes; I will read it and tell you the catchments and every warning."
    await _hand_back(
        result,
        project_id,
        {
            "delineation_id": new_id,
            "mode": chosen,
            "runs_on": job.get("runs_on"),
            "estimate": _estimate_summary(estimate),
        },
        say,
        panel="catchments",
        line=new_id,
    )
    return json.dumps(result, indent=2)


async def _change(
    project_id,
    delineation_id,
    line,
    name,
    start_chainage_m,
    min_area_ha,
    max_divert_area_ha,
    terrain_id,
    mode,
    estimate_only,
    method,
    clean_sources,
) -> str:
    base = f"/projects/{project_id}/delineations/{delineation_id}/"
    if line is not None:
        raise ToolError(
            "A tool cannot move a delineation's line: a person edits the line in the browser (ui_url). Make a new "
            "delineation for a different line. Nothing was sent."
        )
    if terrain_id is not None:
        raise ToolError("A tool cannot change a delineation's terrain: make a new delineation. Nothing was sent.")
    if estimate_only:
        estimate = await client.get(f"{base}estimate/")
        result = {"outcome": "estimate", "delineation_id": delineation_id, "estimate": estimate}
        await _hand_back(
            result,
            project_id,
            {"estimate": _estimate_summary(estimate), "delineation_id": delineation_id},
            _estimate_words(estimate) + " Nothing was run.",
            panel="catchments",
            line=delineation_id,
        )
        return json.dumps(result, indent=2)
    changes: dict = {}
    if name.strip():
        changes["name"] = name.strip()
    for key, value in (
        ("start_chainage_m", start_chainage_m),
        ("min_area_ha", min_area_ha),
        ("max_divert_area_ha", max_divert_area_ha),
    ):
        if value is not None:
            changes[key] = value
    if not changes and mode is None:
        raise ToolError(
            "Nothing to change: give a name, start_chainage_m, min_area_ha or max_divert_area_ha, or mode='full' to "
            "start the full run. Nothing was sent."
        )
    doc = None
    if changes:
        payload = dict(changes)
        if method:
            payload["authored_method"] = method
        if clean_sources:
            payload["sources"] = clean_sources
        body, status = await client.patch(base, json=payload, raise_for_status=False)
        if status >= 500:
            raise ToolError(f"Hydrata API returned {status} for PATCH {base}: {str(body)[:300]}")
        if status >= 400:
            result = _refused(body, status, "the change")
            await _hand_back(
                result,
                project_id,
                {"http_status": status},
                result.pop("say_to_user"),
                panel="catchments",
                line=delineation_id,
            )
            return json.dumps(result, indent=2)
        doc = body
    estimate = None
    run_job = None
    if mode is not None:
        body, status = await client.post(f"{base}run/", json={"mode": mode}, raise_for_status=False)
        if status >= 500:
            raise ToolError(f"Hydrata API returned {status} for POST {base}run/: {str(body)[:300]}")
        if status >= 400:
            result = _refused(body, status, f"the {mode} run")
            await _hand_back(
                result,
                project_id,
                {"http_status": status},
                result.pop("say_to_user"),
                panel="catchments",
                line=delineation_id,
            )
            return json.dumps(result, indent=2)
        run_job = body.get("job") or {}
        doc = await client.get(base)
        estimate = await client.get(f"{base}estimate/")
    result = _result(doc)
    ticks = _ticks_text(doc) if changes else None
    parts = []
    if changes:
        result["changed"] = changes
        result["outcome"] = "changed"
        parts.append(
            "Changed "
            + ", ".join(f"{k} to {_data(str(v)) if k == 'name' else v}" for k, v in changes.items())
            + ": the result was regrouped without a new run (the terrain step was not repeated)."
        )
    if mode is not None:
        result["estimate"] = estimate
        result["http_status"] = 202
        live = (run_job or {}).get("status") in LIVE_STATUSES
        if live:
            result["outcome"] = "started"
            result["next_step"] = (
                f"A full run can take many hours: do not wait on it. Tell the person, and read again later with "
                f"get_delineation(project_id={project_id}, delineation_id={delineation_id})."
                if mode == "full"
                else f"Call get_delineation(project_id={project_id}, delineation_id={delineation_id}) until it is ready."
            )
            if mode == "full":
                parts.append(
                    "The full run has started on AWS Batch at the terrain's own resolution. It can take many hours (up "
                    "to about 24). The quick result stays shown and usable meanwhile, and the person can close the map. "
                    + _estimate_words(estimate)
                )
            else:
                parts.append("The delineation is running again (quick).")
        else:
            result["outcome"] = "cannot_run"
            parts.append(
                f"The {mode} run did not start: {(run_job or {}).get('error') or 'Hydrata gave no reason'} "
                "The quick result is still shown and usable."
            )
    parts.extend(_read_words(doc, result, ticks))
    await _hand_back(
        result,
        project_id,
        {
            "delineation_id": delineation_id,
            "outcome": result["outcome"],
            "catchments": (doc.get("tally") or {}).get("catchments"),
            "total_area_ha": (doc.get("tally") or {}).get("total_area_ha"),
            "diversions_to_check": doc.get("diversions_to_check") or 0,
            "warnings": len(result["warnings"]),
        },
        " ".join(parts),
        panel="catchments",
        line=delineation_id,
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 2: get_delineation
# ---------------------------------------------------------------------------
@_tool("get_delineation")
async def get_delineation(
    project_id: Annotated[int, "The project ID"],
    delineation_id: Annotated[int, "`delineation_id` from delineate_catchments"],
    timeout_seconds: Annotated[
        int,
        "How long this call keeps polling before it returns `timed_out` (default 180, ceiling 200). "
        "0 = one read, no waiting.",
    ] = POLL_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
    wait_for_full_run: Annotated[
        bool,
        "False (default): a quick result is returned as `ready` at once even while a full run is going, with the "
        "full run's progress in `full_run`. True: also wait (bounded) for the full run; a `timed_out` read is "
        "normal for it, since it can take many hours: read again later, do not wait in a loop.",
    ] = False,
) -> str:
    """Read a delineation and wait for its job, the way get_terrain_merge waits for a merge.

    Returns `outcome` (ready | error | timed_out), `phase` (the job's step, then ready or error) and, once there is a
    result: the catchment names, each chainage as CH k+mmm with `distance_m` beside it, areas (saying when one is a
    lower bound), `arrives_ha`, the arrival point, diversion suggestions (information only), the diversions a person
    applied and any marked check again, every warning as its sentence, which neighbours share water, how it was made and
    whether a person confirmed it, the working and terrain resolution and the tally. `timed_out` is normal: call again;
    it never starts a second job. `shown_result` says which result is shown (quick or full); `full_run` reports a full
    run's progress, elapsed time, expected finish, where it runs and whether it was handed to Batch. Read-only.
    """

    async def fetch():
        return await client.get(f"/projects/{project_id}/delineations/{delineation_id}/")

    def waiting_on(doc) -> bool:
        job = (doc or {}).get("job") or {}
        if job.get("status") not in LIVE_STATUSES:
            return False
        return wait_for_full_run or job.get("mode") != "full" or not (doc or {}).get("shown_result")

    cap_spent = _token_cap_spent()
    doc, polls, elapsed, timed_out = await _poll_until(
        fetch, lambda d: not waiting_on(d), timeout_seconds, poll_interval_seconds
    )
    job = doc.get("job") or {}
    status = job.get("status")
    shown = doc.get("shown_result")
    live = status in LIVE_STATUSES
    failed = status in ("error", "cancelled") or (status == "none" and not shown)
    full_job = job.get("mode") == "full"
    if timed_out:
        outcome = "timed_out"
    elif failed and (not shown or not full_job):
        outcome = "error"
    else:
        outcome = "ready"
    phase = _phase_of(job) if (live or outcome == "error") else "ready"
    result = _result(doc)
    result.update({"outcome": outcome, "phase": phase, "polls": polls, "elapsed_seconds": elapsed})
    full_run = None
    if full_job and status != "complete" and shown:
        full_run = _full_run_doc(job)
        result["full_run"] = full_run

    parts = []
    if outcome == "error":
        sentence = job.get("error") or (
            "The run was cancelled." if status == "cancelled" else "Nothing has run on this delineation yet."
        )
        parts.append(
            f"The delineation did not finish: {sentence} Tell the user; delineate_catchments with this delineation_id and mode can run it again."
        )
    elif not shown:
        parts.append(
            f"The delineation is still being made ({phase}, {job.get('progress', 0):.0f}% through, {_duration(job.get('elapsed_s'))} so far); "
            "I am still waiting on it: read it again with get_delineation."
        )
    if shown:
        parts.extend(_read_words(doc, result, _ticks_text(doc)))
    if full_run:
        where = "AWS Batch" if full_run.get("runs_on") == "batch" else "the web server"
        if live:
            parts.append(
                f"A full-resolution run is going on {where}: {full_run.get('progress', 0):.0f}% through, "
                f"{_duration(full_run.get('elapsed_s'))} elapsed"
                + (
                    f", expected to finish about {_duration(full_run.get('expected_remaining_s'))} from now "
                    f"({full_run.get('expected_finish_at')})"
                    if full_run.get("expected_finish_at")
                    else ", with no finish time yet"
                )
                + (", handed over from the web server to Batch" if full_run.get("handed_over") else "")
                + f". The quick result is the one shown ({(shown or {}).get('mode')}) and it is usable meanwhile; the full "
                "result replaces it when it finishes and keeps ids, names and diversions. A full run can take many hours: "
                "do not wait in a loop; tell the person and read again later."
            )
        else:
            parts.append(
                f"The full-resolution run did not finish: {full_run.get('error') or 'it was cancelled'} The quick result "
                "is still shown and usable."
            )
    if timed_out and not shown:
        result["next_step"] = f"Call get_delineation(project_id={project_id}, delineation_id={delineation_id}) again."
        if cap_spent:
            result["token_refresh_pending"] = True
            result["next_step"] = TOKEN_REFRESH_NEXT_STEP.format(tool="get_delineation")
    elif live:
        result["next_step"] = (
            f"Read again later with get_delineation(project_id={project_id}, delineation_id={delineation_id}); a full run can "
            "take many hours, so do not wait in a loop."
        )
        if cap_spent and timed_out:
            result["token_refresh_pending"] = True
            result["next_step"] = TOKEN_REFRESH_NEXT_STEP.format(tool="get_delineation")
    summary = {
        "delineation_id": delineation_id,
        "phase": phase,
        "catchments": (doc.get("tally") or {}).get("catchments"),
        "total_area_ha": (doc.get("tally") or {}).get("total_area_ha"),
        "warnings": len(result["warnings"]),
        "diversions_to_check": doc.get("diversions_to_check") or 0,
        "shown_result": (shown or {}).get("mode"),
    }
    await _hand_back(result, project_id, summary, " ".join(parts), panel="catchments", line=delineation_id)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 3: use_catchments_as_boundary
# ---------------------------------------------------------------------------
@_tool("use_catchments_as_boundary")
async def use_catchments_as_boundary(
    project_id: Annotated[int, "The project ID (Editor rights)"],
    delineation_id: Annotated[int, "`delineation_id` from delineate_catchments"],
    catchment_ids: Annotated[
        list[int],
        "The ids (`id` in `catchments`) of the catchments whose outer edge is the model boundary. They must touch "
        "each other: one model has one boundary ring.",
    ],
    authored_method: Annotated[
        str, "One short phrase saying why these catchments are the model area. Shown to the person who reviews it."
    ] = "",
    sources: Annotated[list[dict] | None, SOURCES_ARGUMENT_HELP] = None,
) -> str:
    """Use chosen catchments as the model boundary: make a new boundary input round them.

    The boundary is Closed (Reflective) everywhere except a short open stretch, typed Transmissive, at each included
    drainage point. It is a NEW boundary input of the project (nothing is overwritten) and is unreviewed, as is the
    delineation while a person has not confirmed it: nothing is built or run until a person reviews them. The
    delineation itself is not changed. Attach the boundary to a scenario with update_scenario(boundary=<id>).
    Catchments that do not touch each other are refused in words. Do not draw a model boundary off the platform.
    """
    ids = catchment_ids if isinstance(catchment_ids, list) else []
    if not ids or any(isinstance(i, bool) or not isinstance(i, int) for i in ids) or len(set(ids)) != len(ids):
        raise ToolError(
            "catchment_ids must list one or more different catchment ids (the `id` of each catchment). Nothing was sent."
        )
    cited = _clean_sources(sources)
    payload: dict = {"catchments": sorted(ids)}
    if authored_method.strip():
        payload["authored_method"] = authored_method.strip()
    if cited:
        payload["sources"] = cited
    base = f"/projects/{project_id}/delineations/{delineation_id}/"
    body, status = await client.post(f"{base}boundary/", json=payload, raise_for_status=False)
    if status >= 500:
        raise ToolError(f"Hydrata API returned {status} for POST {base}boundary/: {str(body)[:300]}")
    if status >= 400:
        result = _refused(body, status, "the model boundary")
        await _hand_back(
            result,
            project_id,
            {"http_status": status},
            result.pop("say_to_user"),
            panel="catchments",
            line=delineation_id,
        )
        return json.dumps(result, indent=2)
    doc = await client.get(base)
    result = _result(doc)
    made = body if isinstance(body, dict) else {}
    boundary = made.get("boundary") or {}
    names = _names(doc)
    used = [_data(str(names.get(i, i))) for i in made.get("catchment_ids") or ids]
    result.update(
        {
            "outcome": "made",
            "http_status": status,
            "boundary": boundary,
            "catchment_ids": made.get("catchment_ids"),
            "made_by": made.get("made_by"),
            "made_by_agent": made.get("made_by_agent"),
            "made_at": made.get("made_at"),
            "method": made.get("method"),
            "open_stretch_m": made.get("open_stretch_m"),
            "open_stretches": made.get("open_stretches") or [],
            "perimeter_m": made.get("perimeter_m"),
            "boundary_rule": BOUNDARY_RULE,
        }
    )
    d = doc.get("delineation") or {}
    unreviewed_delineation = bool(d.get("agent_authored") and not d.get("confirmed"))
    stretches = made.get("open_stretches") or []
    open_total = sum(x.get("length_m") or 0 for x in stretches if isinstance(x, dict))
    say = (
        f"Made boundary '{_data(str(boundary.get('title')))}' (input {boundary.get('id')}, {boundary.get('feature_count')} "
        f"features, a ring {_m(made.get('perimeter_m'))} m round) from catchments {', '.join(used)}. It is Closed "
        f"(Reflective) everywhere except short open stretches, typed Transmissive, at each included drainage point "
        f"({len(stretches)} stretch{'es' if len(stretches) != 1 else ''}, {_m(open_total)} m in all; each about "
        f"{_m(made.get('open_stretch_m'))} m, and stretches that overlap join into one). The boundary is unreviewed"
        + (", and so is the delineation (no person has confirmed it)" if unreviewed_delineation else "")
        + ": nothing is built or run until a person reviews them in the browser. To model with it, attach it to a "
        f"scenario with update_scenario(boundary={boundary.get('id')}), then give the user the link."
    )
    parts = [say]
    ticks = _ticks_text(doc)
    if ticks:
        parts.append(ticks)
    words = result["warnings_in_words"]
    if words:
        parts.append(
            "Warnings about this delineation, relay every one: "
            + " ".join(f"({i}) {w}" for i, w in enumerate(words, start=1))
        )
    await _hand_back(
        result,
        project_id,
        {
            "boundary_id": boundary.get("id"),
            "delineation_id": delineation_id,
            "catchment_ids": made.get("catchment_ids"),
            "open_stretch_m": made.get("open_stretch_m"),
            "perimeter_m": made.get("perimeter_m"),
            "unreviewed": boundary.get("unreviewed"),
        },
        " ".join(parts),
        panel="catchments",
        line=delineation_id,
    )
    return json.dumps(result, indent=2)
