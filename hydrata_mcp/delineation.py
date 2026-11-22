"""TASK-4404 (epic 4274 W4.1) - catchments from a delineation line, through three tools.

delineate_catchments  - a line on the project's terrain makes a delineation and starts its job (returns at once, with
                        the estimate); with a delineation id it changes the two areas, the name or the start chainage
                        of the agent's own unconfirmed delineation (a regroup, no new job) or starts its High resolution
                        run. Over a fast screen, High resolution makes a COPY of the line ('<name> (high res)') and runs
                        there; the fast screen stays for comparison (operator, 15-11-26).
get_delineation       - reads one, waiting for its job the way get_terrain_merge waits for a merge.
use_catchments_as_boundary - a Closed (Reflective) model boundary round chosen catchments, with a short Transmissive
                        opening at each drainage point (decisions D14 = A, D15 = A).
adopt_catchments      - (epic 4445 W6.2) a CONFIRMED line's catchments published as a GeoNode layer of the project: a
                        snapshot, marked when the line has changed since; adopting again replaces its contents (D8, D16).

Every result carries the API's own field names (TASK-4397, 4398, 4410, 4417) and, beside each chainage written
CH k+mmm, its distance in metres. Every warning comes back as its sentence. The tools never tick, apply or undo a
diversion and never confirm: only a person does, in the browser (decision D13). What an agent makes is unreviewed
until a person confirms it. There is no tool that deletes a delineation or edits its line.

Words (operator, 15-11-26): the quick run (API mode 'quick') is the FAST SCREEN (low resolution), a first look; the full run
(mode 'full') is HIGH RESOLUTION. A fast screen is a quick result that was coarsened; its ground profile is not given anywhere.
TASK-4567 (operator, 21-11-26): the fast screen is optional, not necessary: a new line may go straight to High resolution, on
that line itself (no copy).
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
from .client import HydrataAPIError
from .terrain import _lon_lat_pairs

LIVE_STATUSES = ("pending", "running")
# What stays on the screen: the drawing data. The agent reads the ground itself with terrain_profile.
OMITTED = "Profile, polygons and line vertices stay on the map. Use terrain_profile for the ground along a line."
FAST_SCREEN = "fast screen (low resolution)"
HIGH_RES = "high resolution"
FAST_SCREEN_PROFILE = "Fast screen: no ground profile anywhere. Run High resolution to see it."
FAST_SCREEN_CAVEAT = (
    "Fast screen: a first look at low resolution. Say so and offer High resolution. It can still be the model boundary."
)
FAST_SCREEN_LUMPED = (
    "A fast screen shows one catchment per section of the line, with no smaller catchments, diversion suggestions or "
    "shared-water points: High resolution gives those."
)
SUGGESTIONS_NOTE = "Diversion suggestions are information only. Only a person ticks one, in the browser."
BOUNDARY_RULE = (
    "Closed (Reflective) except a short Transmissive stretch at each included drainage point (5 working cells, 20 m "
    "minimum). The line itself is not open."
)
PERSON_ONLY = "Only a person can tick or check a diversion, or confirm a delineation."
# TASK-4545: a run on a confirmed line replaces the result a person confirmed (the Catchments pane's words before Run again)
CONFIRMED_LAPSES = "This line is confirmed. Running it again lapses the confirmation."
# TASK-4456 (epic 4445 W2.7): the terrain's drainage analysis, being prepared or out of date. The API's ``drainage_analysis``
# carries the sentence (a line's read for its run's grid; the estimate per grid); these are the same words, for an API that
# sends a state without one.
DRAINAGE_WORDS = {
    "being_prepared": "Drainage analysis is being prepared. Runs wait for it.",
    "out_of_date": "Drainage analysis is out of date. Next run takes longer.",
}


def _with_fix(doc) -> str | None:
    """A warning's or refusal's sentence with what to do about it (the API's `fix_sentence`, TASK-4535) after it."""
    if not isinstance(doc, dict):
        return None
    sentence = doc.get("sentence") or doc.get("detail")
    if not isinstance(sentence, str) or not sentence:
        return None
    fix = doc.get("fix_sentence")
    return f"{sentence} {fix}" if isinstance(fix, str) and fix else sentence


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
            f"Area is at least {_m(c.get('area_ha'))} ha: terrain ends inside this catchment. Add terrain past the edge."
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
        said = (
            f"{pair}: {height:.2f} m rise between them upstream of the line"
            if measured
            else f"{pair} can share water upstream of the line"
        )
    else:
        at = _chainage_text(item.get("pass_chainage_m"))
        said = f"{f'{at}: ' if at else ''}{pair} can share water" + (
            f" after {height:.2f} m ponding" if measured else ""
        )
    return f"{said}. Areas uncertain."


def _meets_twice_sentence(item: dict, names: dict) -> str:
    """The API's meets-twice wording (TASK-4405): the catchment first, where its water reaches the line again, what follows."""
    here = _data(str(names.get(item.get("catchment"), item.get("catchment"))))
    again = _data(str(names.get(item.get("meets_again_in"), item.get("meets_again_in"))))
    return (
        f"{here}: outflow reaches the line again at {_chainage_text(item.get('chainage_m'))}, in {again}. "
        "Its area is not counted there."
    )


def _ticks_text(doc: dict) -> str | None:
    to_check = doc.get("diversions_to_check") or 0
    remembered = [d for d in doc.get("remembered_diversions") or [] if d.get("needs_check")]
    if not to_check and not remembered:
        return None
    parts = []
    if to_check:
        parts.append(
            "1 ticked diversion to check again" if to_check == 1 else f"{to_check} ticked diversions to check again"
        )
    if remembered:
        parts.append(
            f"{len(remembered)} more tick{'' if len(remembered) == 1 else 's'} remembered, not applied at these settings"
        )
    return "; ".join(parts) + f". {PERSON_ONLY} Give the user the link. It cannot be confirmed until they are checked."


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


def _is_fast_screen(shown) -> bool:
    """The API's `fast_screen`; for an older API, a quick result that was coarsened (or does not say)."""
    if not isinstance(shown, dict) or not shown:
        return False
    if "fast_screen" in shown:
        return bool(shown.get("fast_screen"))
    return (shown.get("mode") or "quick") == "quick" and shown.get("coarsened") is not False


def _kind(shown) -> str:
    """'fast screen (low resolution)' or 'high resolution', for a sentence."""
    return FAST_SCREEN if _is_fast_screen(shown) else HIGH_RES


def _copied_from_text(line: dict) -> str | None:
    """'High resolution copy of delineation 7 (<name>)...' for a copy, else None. The name is a person's words."""
    source = line.get("derived_from") if isinstance(line, dict) else None
    if not isinstance(source, dict) or source.get("id") is None:
        return None
    return (
        f"High resolution copy of delineation {source.get('id')} ({_data(str(source.get('name')))}). "
        "Its fast screen is kept."
    )


def _drainage_sentence(status) -> str | None:
    """The one sentence about the terrain's drainage analysis, when it is being prepared or out of date; else None."""
    if not isinstance(status, dict) or status.get("state") not in DRAINAGE_WORDS:
        return None
    return str(status.get("sentence") or DRAINAGE_WORDS[status["state"]]).strip()


def _phase_of(job: dict) -> str:
    status = job.get("status")
    if status in LIVE_STATUSES:
        return job.get("step") or ("queued" if status == "pending" else "running")
    return "error" if status in ("error", "cancelled") else "ready"


SIDE_ARGUMENT_HELP = (
    "Upstream side looking from the line's start to its end: 'left', 'right', or 'automatic' (default: each section "
    "takes the side most ground arrives from). Pass a side only when the person said which side the water comes from. "
    "The reply names the side per section."
)
SIDES_NOT_AVAILABLE = (
    "No upstream-side sections: this result predates them. Run it again (delineate_catchments with this "
    "delineation_id and mode)."
)


def _sections_doc(doc: dict) -> list | None:
    """The API's sections (None for a result made before sides), each with its chainages as text; no geometry."""
    sections = doc.get("sections")
    if sections is None:
        return None
    out = []
    for s in sections:
        item = {k: v for k, v in s.items() if k != "geometry"}
        out.append(_with_text(item, ("from_chainage_m", "from_chainage"), ("to_chainage_m", "to_chainage")))
    return out


def _sides_words(doc: dict) -> str | None:
    """The side used for each section, the ground left out, and who chose the sides, in one paragraph (None with no result)."""
    if not doc.get("shown_result"):
        return None
    sections = doc.get("sections")
    if sections is None:
        return SIDES_NOT_AVAILABLE
    if not sections:
        return None
    each = "; ".join(
        f"{_chainage_text(s.get('from_chainage_m'))} to {_chainage_text(s.get('to_chainage_m'))}: {s.get('side')}, "
        f"{float(s.get('taken_ha') or 0):,.1f} ha in, {float(s.get('left_out_ha') or 0):,.1f} ha out"
        for s in sections
    )
    count = len(sections)
    sides = doc.get("sides") or {}
    who = "Sides set by hand." if sides.get("set_by_hand") else "Sides chosen automatically."
    words = f"Upstream side, {count} section{'' if count == 1 else 's'}: {each}. {who}"
    left_out = (doc.get("ground_left_out") or {}).get("sentence")
    if left_out:
        words += f" {left_out}"
    return words


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
        "sections": _sections_doc(doc),
        "sides": doc.get("sides"),
        "ground_left_out": doc.get("ground_left_out"),
        "ignored_diversions": doc.get("ignored_diversions") or [],
        "job": doc.get("job"),
        "drainage_analysis": doc.get("drainage_analysis"),
        # epic 4445 W5.1: the terrain's drainage corrections changed since this line was run (the API's sentence)
        "drainage_changed": doc.get("drainage_changed"),
        # epic 4445 W6.2: the line's adopted layer and whether the line has changed since (null: never adopted)
        "adopted_layer": _adopted_doc(doc.get("promoted_layer")),
        # epic 4445 W11.3 (TASK-4485): what adopting now carries of the line's confirmed sub-catchments, and what it leaves
        # out (a draft) as the API's sentence (null: the line has none)
        "adopt_sub_catchments": doc.get("adopt_sub_catchments"),
        "omitted": OMITTED,
    }
    shown = doc.get("shown_result")
    if shown:
        out["resolution"] = _kind(shown)
        out["profile_available"] = not _is_fast_screen(shown)
        if _is_fast_screen(shown):
            out["omitted"] = f"{OMITTED} {FAST_SCREEN_PROFILE}"
    source = line.get("derived_from")
    if isinstance(source, dict) and source.get("id") is not None:
        out["derived_from"] = {"id": source.get("id"), "name": _data(str(source.get("name")))}
    out["warnings_in_words"] = [_with_fix(w) for w in out["warnings"] if w.get("sentence")]
    return out


# TASK-4552 (operator, Gate 2b): the word is adopt. The API keeps its own names (a route, three keys and an error code, all
# identifiers); an agent is sent the adopt words for them.
ADOPT_WORDS = {
    "promoted_layer": "adopted_layer",
    "promotions": "adoptions",
    "promoted_catchments": "adopted_catchments",
    "PROMOTED_ALREADY": "ADOPTED_ALREADY",
}


def _adopt_words(value):
    """``value`` with the API's promote names (keys, and the kind and error code values) as the adopt words."""
    if isinstance(value, dict):
        return {ADOPT_WORDS.get(k, k): _adopt_words(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_adopt_words(v) for v in value]
    return ADOPT_WORDS.get(value, value) if isinstance(value, str) else value


def _adopted_doc(layer) -> dict | None:
    """The API's adopted layer, with the names a person typed wrapped as data."""
    if not isinstance(layer, dict):
        return None
    out = _adopt_words(layer)
    for key in ("title", "line_name", "made_by"):
        if out.get(key) is not None:
            out[key] = _data(str(out[key]))
    return out


def _adopted_words(layer) -> str | None:
    """One sentence on the line's adopted layer, and the API's own sentences (the line has changed since; an agent
    adopted it and no person has reviewed it), to relay."""
    if not isinstance(layer, dict):
        return None
    who = "an agent" if layer.get("made_by_agent") else layer.get("made_by")
    text = f"Adopted as layer {layer.get('title')} ({layer.get('alternate')}) by {who} on {layer.get('made_at')}."
    if layer.get("layer_exists") is False:
        text += " Layer deleted since; adopt_catchments remakes it."
    fixes = layer.get("fix_sentences") or []
    sentences = [
        f"{x} {fixes[i]}" if i < len(fixes) and isinstance(fixes[i], str) and fixes[i] else x
        for i, x in enumerate(layer.get("sentences") or [])
        if isinstance(x, str) and x
    ]
    if sentences:
        text += " Relay: " + " ".join(sentences)
    elif not layer.get("line_changed_since"):
        text += " Line unchanged since."
    return text


def _who(doc: dict) -> str:
    d = doc.get("delineation") or {}
    if d.get("confirmed"):
        return f"Confirmed by {_data(str(d.get('confirmed_by')))} on {d.get('confirmed_at')}."
    if d.get("agent_authored"):
        return "Made by an agent, unreviewed. Only a person confirms it, in the browser."
    lapse = _lapse_sentence(doc)
    if lapse:
        return f"Made by {_data(str(d.get('created_by')))}. Not confirmed. {lapse} Only a person confirms it again, in the browser."
    return f"Made by {_data(str(d.get('created_by')))}. Not confirmed."


def _lapse_sentence(doc: dict) -> str | None:
    """Epic 4445 W12.5 (TASK-4579, TASK-4598): why a person's confirmation lapsed, the API's sentence (a person's diversion
    tick keeps it; a re-run, a new terrain, a moved line, a Minimum catchment area or side change, or diversions to check
    again lapse it). The API keeps it (``delineation.confirmation_lapsed``) until a person confirms again; else None."""
    lapse = (doc.get("delineation") or {}).get("confirmation_lapsed")
    sentence = lapse.get("sentence") if isinstance(lapse, dict) else None
    return sentence if isinstance(sentence, str) and sentence else None


def _read_words(doc: dict, result: dict, ticks: str | None = None) -> list[str]:
    line, tally = doc.get("line") or {}, doc.get("tally") or {}
    shown = doc.get("shown_result") or {}
    parts = []
    if shown:
        count = tally.get("catchments")
        diverted = tally.get("diverted", 0) or 0
        # every suggestion listed; the API's tally.suggested counts only the shallow ones (a drain 2 m deep or less)
        suggested = len(doc.get("suggestions") or [])
        shallow = tally.get("suggested", 0) or 0
        kind = _kind(shown)
        parts.append(
            f"Delineation {line.get('id')} ({_data(str(line.get('name')))}): {count} catchment{'' if count == 1 else 's'}, "
            f"{tally.get('total_area_ha', 0):.1f} ha. {diverted} diversion{'' if diverted == 1 else 's'} applied, "
            f"{suggested} suggested, {shallow} with a drain 2 m deep or less. {kind[0].upper()}{kind[1:]} at "
            f"{_m(shown.get('working_resolution_m'))} m on a {_m(shown.get('terrain_resolution_m'))} m terrain."
        )
        if _is_fast_screen(shown):
            parts.append(f"{FAST_SCREEN_CAVEAT} {FAST_SCREEN_PROFILE}")
        if shown.get("lumped"):
            parts.append(FAST_SCREEN_LUMPED)
    sides = _sides_words(doc)
    if sides:
        parts.append(sides)
    copied = _copied_from_text(line)
    if copied:
        parts.append(copied)
    parts.append(_who(doc))
    adopted = _adopted_words(result.get("adopted_layer"))
    if adopted:
        parts.append(adopted)
    left_out = (result.get("adopt_sub_catchments") or {}).get("left_out_sentence")
    if left_out:
        parts.append(f"Adopting: {left_out}")
    if ticks:
        parts.append(ticks)
    words = result["warnings_in_words"]
    if words:
        parts.append("Warnings, relay every one: " + " ".join(f"({i}) {w}" for i, w in enumerate(words, start=1)))
    else:
        parts.append("No warnings.")
    if result["shares_water"]:
        parts.append(f"{len(result['shares_water'])} pair(s) can share water. Their areas are uncertain.")
    replaced = shown.get("replaced") if shown.get("mode") == "full" else None
    if isinstance(replaced, dict):
        lost = (replaced.get("not_carried_over") or {}).get("catchments") or []
        if lost:
            parts.append(f"New high resolution result. {len(lost)} earlier catchment(s) have no match.")
        if replaced.get("confirmation_lapsed") and not _lapse_sentence(doc):  # the stored reason is said in _who
            parts.append("Confirmation lapsed. A person must confirm again.")
    return parts


MODE_WORDS = {"quick": "Fast screen (mode 'quick', low resolution)", "full": "High resolution (mode 'full')"}


def _estimate_words(estimate: dict) -> str:
    out = []
    for mode in ("quick", "full"):
        e = estimate.get(mode) or {}
        low = (e.get("expected_seconds") or {}).get("low")
        high = (e.get("expected_seconds") or {}).get("high")
        where = "AWS Batch" if e.get("runs_on") == "batch" else "the web server"
        if e.get("drainage_analysis_first"):
            # TASK-4559: the run waits for the terrain's drainage analysis first (the range already holds the wait), as
            # the Catchments footer says
            wait = e.get("drainage_analysis_seconds") or {}
            first = "AWS Batch" if e.get("drainage_analysis_runs_on") == "batch" else "the web server"
            where += (
                f", after the drainage analysis (prepared first on {first}, "
                f"{_duration(wait.get('low'))} to {_duration(wait.get('high'))})"
            )
        text = (
            f"{MODE_WORDS[mode]}: {e.get('cells'):,} cells at {_m(e.get('working_resolution_m'))} m on {where}, "
            f"{_duration(low)} to {_duration(high)}"
            f"{', can take hours' if e.get('can_take_hours') else ''}"
        )
        if e.get("may_move_to_batch"):
            text += " (moves to Batch if it overruns)"
        text += "."
        if not e.get("can_run"):
            reason = str(e.get("cannot_run_reason") or "").strip()
            text += f" The {MODE_WORDS[mode]} run CANNOT run here: {reason}{'' if reason.endswith('.') else '.'}"
        out.append(text)
        drainage = _drainage_sentence(e.get("drainage_analysis"))
        if drainage:
            out.append(f"{MODE_WORDS[mode]}: {drainage}")
    note = (estimate.get("quick") or {}).get("note")
    copy = (
        " High resolution runs on a copy, '<name> (high res)'. The fast screen stays."
        if estimate.get("full_makes_copy")
        else ""
    )
    return "Estimate (a range). " + " ".join(out) + (f" {note}" if note else "") + copy


def _refused(body, status: int, what: str, project_id=None, asked_id=None) -> dict:
    reply = body if isinstance(body, dict) else {"response": body}
    sentence = _with_fix(reply) or reply.get("error") or reply.get("error_code") or f"HTTP {status}"
    out = {
        "outcome": "refused",
        "http_status": status,
        "error_code": reply.get("error_code"),
        "hydrata_reply": reply,
        "say_to_user": (
            f"Refused {what}: {sentence} Nothing changed. For a person's or a confirmed delineation, ask the user to "
            "change it in the browser."
        ),
    }
    running = reply.get("delineation_id")
    if running is not None and running != asked_id:
        # a High resolution copy of this line is already running: follow it rather than starting another
        out["delineation_id"] = running
        out["next_step"] = (
            f"Read the running copy with get_delineation(project_id={project_id}, delineation_id={running})."
        )
        out["say_to_user"] = (
            f"Refused {what}: {sentence} Nothing changed. Follow delineation {running} with get_delineation."
        )
    return out


def _estimate_summary(estimate: dict) -> dict:
    return {
        m: {
            **{k: (estimate.get(m) or {}).get(k) for k in ("cells", "runs_on", "can_run", "expected_seconds")},
            "drainage_analysis": ((estimate.get(m) or {}).get("drainage_analysis") or {}).get("state"),
        }
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
        "mode='full', to start High resolution (on a copy of the line when its result is a fast screen; on the line "
        "itself when it has no result). A "
        "delineation a person made or confirmed is refused.",
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
        "'quick' (the default for a new delineation) is the FAST SCREEN: low resolution, coarsened to the web "
        "server's limit, seconds to a couple of minutes; an optional first look. 'full' is HIGH RESOLUTION: the "
        "terrain's own resolution; a big terrain runs on AWS Batch and can take many hours (up to about 24), and the "
        "person can close the map. The fast screen is optional: a new line can go straight to 'full', on that line "
        "itself (no copy); say its estimate first (estimate_only). With delineation_id, 'full' starts High "
        "resolution: over a fast screen it makes a COPY of the line, '<name> (high res)', and runs there (the fast "
        "screen stays, for comparison; read the copy's delineation_id with get_delineation); 'quick' runs the fast "
        "screen again.",
    ] = None,
    side: Annotated[Literal["left", "right", "automatic"] | None, SIDE_ARGUMENT_HELP] = None,
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

    The line has an upstream side for each section (`side`): Hydrata adopts the side most ground arrives from unless
    you pass one, and ground on a section's downstream side is left out (one sentence says how much). Every reply with a
    result names the side used per section; relay it.

    With `delineation_id`: changing the name, start chainage, either area or the side regroups the stored result without
    repeating the terrain step (no new job). A person's diversion ticks survive the change: they are re-applied and
    marked check again until a person checks them; say so. A delineation a person made or confirmed is refused in
    words. `mode='full'` starts High resolution: when the line's result is a fast screen (low resolution), Hydrata
    makes a COPY of the line, '<name> (high res)', and runs there, so the two can be compared; the result then names
    the copy's `delineation_id` (`created_copy` true): read THAT one with get_delineation.

    The fast screen is optional: a NEW delineation with `mode='full'` goes straight to High resolution, on that line
    itself (no copy, the name as given). Say its estimate first (`estimate_only`).
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
            side,
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
            + " Nothing was run. The fast screen is optional: offer High resolution with this estimate, and the fast "
            "screen as a first look if the person wants one.",
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
            f"{MODE_WORDS[chosen]} cannot start. No delineation was made: {sentence} "
            + (
                "Do the fast screen instead (mode='quick'): " + _estimate_words(estimate)
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
        ("side", side),
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
        f"{MODE_WORDS[chosen]} delineation '{_data(name.strip())}' (id {new_id}) started on {where}. "
        "Made by an agent, unreviewed: only a person confirms it. " + _estimate_words(estimate)
    )
    say += (
        f" Upstream side: {side}, as asked."
        if side in ("left", "right")
        else " Upstream side: automatic. The result names the side per section."
    )
    if chosen == "full":
        say += " High resolution, on this line (no copy). No fast screen is made: it is optional."
        if (estimate.get("full") or {}).get("can_take_hours"):
            say += " It can take many hours. The person can close the map."
    else:
        say += (
            " The fast screen takes seconds to minutes. It is a first look: offer High resolution before the person "
            "relies on it."
        )
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
    side=None,
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
        ("side", side),
    ):
        if value is not None:
            changes[key] = value
    if not changes and mode is None:
        raise ToolError(
            "Nothing to change: give a name, start_chainage_m, min_area_ha, max_divert_area_ha or side, or mode='full' "
            "to start the full run. Nothing was sent."
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
    copy_id = None
    read_id = delineation_id
    if mode is not None:
        body, status = await client.post(f"{base}run/", json={"mode": mode}, raise_for_status=False)
        if status >= 500:
            raise ToolError(f"Hydrata API returned {status} for POST {base}run/: {str(body)[:300]}")
        if status >= 400:
            result = _refused(body, status, f"the {MODE_WORDS[mode]} run", project_id, delineation_id)
            say = result.pop("say_to_user")
            if status == 403:
                # the person is sent to the browser to run it: when the line is confirmed, tell them what that costs
                line_doc, read_status = await client.get_with_status(base)
                if read_status == 200 and (line_doc.get("delineation") or {}).get("confirmed"):
                    result["confirmed"] = True
                    say += f" {CONFIRMED_LAPSES}"
            await _hand_back(
                result,
                project_id,
                {"http_status": status},
                say,
                panel="catchments",
                line=delineation_id,
            )
            return json.dumps(result, indent=2)
        run_job = body.get("job") or {}
        if body.get("created_copy") and body.get("delineation_id") not in (None, delineation_id):
            # High resolution over a fast screen: Hydrata made a copy of the line and runs there; the fast screen is untouched
            copy_id = read_id = body.get("delineation_id")
        read_base = f"/projects/{project_id}/delineations/{read_id}/"
        doc = await client.get(read_base)
        estimate = await client.get(f"{read_base}estimate/")
    result = _result(doc)
    ticks = _ticks_text(doc) if changes else None
    parts = []
    if changes:
        result["changed"] = changes
        result["outcome"] = "changed"
        parts.append(
            "Changed "
            + ", ".join(f"{k} to {_data(str(v)) if k == 'name' else v}" for k, v in changes.items())
            + ": regrouped without a new run."
        )
    if mode is not None:
        result["estimate"] = estimate
        result["http_status"] = 202
        result["created_copy"] = copy_id is not None
        if copy_id is not None:
            result["delineation_id"] = copy_id
            result["source_delineation_id"] = delineation_id
            copy_name = _data(str((doc.get("line") or {}).get("name")))
            parts.append(
                f"High resolution runs on a copy, {copy_name} (delineation {copy_id}). The fast screen (delineation "
                f"{delineation_id}) stays. Read delineation {copy_id} from now on."
            )
        live = (run_job or {}).get("status") in LIVE_STATUSES
        if live:
            result["outcome"] = "started"
            result["next_step"] = (
                f"High resolution can take many hours: do not wait on it. Tell the person, and read again later with "
                f"get_delineation(project_id={project_id}, delineation_id={read_id})."
                if mode == "full"
                else f"Call get_delineation(project_id={project_id}, delineation_id={read_id}) until it is ready."
            )
            if mode == "full":
                where = "AWS Batch" if (run_job or {}).get("runs_on") == "batch" else "the web server"
                parts.append(
                    f"High resolution started on {where}. It can take up to about 24 h. The person can close the map."
                    + ("" if copy_id is not None else " The result shown stays usable until the new one replaces it.")
                    + " "
                    + _estimate_words(estimate)
                )
            else:
                parts.append("Fast screen running again.")
            # the run's own answer holds the line as it stood when the run started; a copy leaves its source confirmed
            if copy_id is None and (body.get("delineation") or {}).get("confirmed"):
                result["confirmation_lapses"] = True
                parts.append(CONFIRMED_LAPSES)
        else:
            result["outcome"] = "cannot_run"
            parts.append(
                f"{MODE_WORDS[mode]} did not start: {(run_job or {}).get('error') or 'Hydrata gave no reason.'} "
                + (
                    f"Copy {copy_id} has no result. Fast screen {delineation_id} is unchanged."
                    if copy_id is not None
                    else "Result unchanged."
                )
            )
    parts.extend(_read_words(doc, result, ticks))
    drainage = _drainage_sentence(doc.get("drainage_analysis"))
    if drainage and drainage not in " ".join(parts):
        parts.append(drainage)
    await _hand_back(
        result,
        project_id,
        {
            "delineation_id": read_id,
            **({"created_copy": True, "source_delineation_id": delineation_id} if copy_id is not None else {}),
            "outcome": result["outcome"],
            "catchments": (doc.get("tally") or {}).get("catchments"),
            "total_area_ha": (doc.get("tally") or {}).get("total_area_ha"),
            "diversions_to_check": doc.get("diversions_to_check") or 0,
            "warnings": len(result["warnings"]),
        },
        " ".join(parts),
        panel="catchments",
        line=read_id,
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
        "How long this call keeps polling before it returns `timed_out` (default 110, ceiling 110). "
        "0 = one read, no waiting.",
    ] = POLL_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[float, "Seconds between polls (default 5)"] = 5.0,
    wait_for_full_run: Annotated[
        bool,
        "False (default): a result already shown is returned as `ready` at once even while a High resolution run "
        "goes over it (a high resolution line run again), with that run's progress in `full_run`. True: also wait "
        "(bounded) for it; a `timed_out` read is normal, since it can take many hours: read again later, do not wait "
        "in a loop. (High resolution over a fast screen runs on a COPY of the line: read the copy's own id.)",
    ] = False,
) -> str:
    """Read a delineation and wait for its job, the way get_terrain_merge waits for a merge.

    Returns `outcome` (ready | error | timed_out), `phase` (the job's step, then ready or error) and, once there is a
    result: the catchment names, each chainage as CH k+mmm with `distance_m` beside it, areas (saying when one is a
    lower bound), `arrives_ha`, the arrival point, diversion suggestions (information only), the diversions a person
    applied and any marked check again, every warning as its sentence, which neighbours share water, how it was made and
    whether a person confirmed it (a person's diversion tick keeps a confirmation; when a change to the catchments lapsed
    it, `delineation.confirmation_lapsed` says why until a person confirms again), the working and terrain resolution and
    the tally. `timed_out` is normal: call again;
    it never starts a second job. `resolution` says plainly whether the result is a 'fast screen (low resolution)' (a
    first look: say so, and offer High resolution before the person relies on it; its profile is not available) or
    'high resolution'; `shown_result` carries the API's fields (mode quick or full, fast_screen, label). A High
    resolution copy of a line says which line it came from (`derived_from`). `full_run` reports a High resolution run's
    progress, elapsed time, expected finish, where it runs and whether it was handed to Batch. `drainage_analysis` says
    whether the terrain's drainage analysis is being prepared (the run waits for it) or out of date (the next run works it
    out again), with the sentence to relay. Read-only.
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
        sentence = job.get("error") or ("Run cancelled." if status == "cancelled" else "Nothing has run yet.")
        parts.append(
            f"Run did not finish: {sentence} Tell the user. delineate_catchments with this delineation_id and mode runs "
            "it again."
        )
    elif not shown:
        parts.append(
            f"{'High resolution' if full_job else 'Fast screen'}: {job.get('progress', 0):.0f}% processed. "
            f"{_duration(job.get('elapsed_s'))} ({phase}). Read again with get_delineation."
        )
    if shown:
        parts.extend(_read_words(doc, result, _ticks_text(doc)))
    if full_run:
        where = "AWS Batch" if full_run.get("runs_on") == "batch" else "the web server"
        if live:
            parts.append(
                f"High resolution on {where}: {full_run.get('progress', 0):.0f}% processed. "
                f"{_duration(full_run.get('elapsed_s'))}"
                + (
                    f", finish in about {_duration(full_run.get('expected_remaining_s'))} "
                    f"({full_run.get('expected_finish_at')})"
                    if full_run.get("expected_finish_at")
                    else ", no finish time yet"
                )
                + (", moved from the web server to Batch" if full_run.get("handed_over") else "")
                + f". The result shown ({_kind(shown)}) stays usable; the new one keeps ids, names and diversions. "
                "Do not wait in a loop: tell the person and read again later."
            )
        else:
            parts.append(
                f"High resolution did not finish: {full_run.get('error') or 'Run cancelled.'} The result shown "
                f"({_kind(shown)}) is unchanged."
            )
    drainage = _drainage_sentence(doc.get("drainage_analysis"))
    if drainage:
        parts.append(drainage)
    changed = doc.get("drainage_changed")
    if isinstance(changed, dict) and changed.get("sentence"):
        parts.append(_with_fix(changed))
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
        "resolution": _kind(shown) if shown else None,
        "drainage_analysis": (doc.get("drainage_analysis") or {}).get("state"),
    }
    await _hand_back(result, project_id, summary, " ".join(parts), panel="catchments", line=delineation_id)
    return json.dumps(result, indent=2)


# The API's boundary notes (TASK-4430), written here from its data so a catchment's name (a person may have typed it) is wrapped
ENCLOSED_NOTE = "{ha:.1f} ha inside the boundary is not in a ticked catchment."
OPENING_NOTE = "{name} keeps its opening. Its diversion is not modelled."


def _boundary_notes(made: dict) -> tuple[list, list]:
    """(notes, diverted openings) for a made boundary: the ground the ring takes in that no ticked catchment holds, and each
    ticked catchment with an applied diversion, which keeps its own opening (the diversion is not modelled). Names as data."""
    notes, openings = [], []
    enclosed = made.get("enclosed_not_ticked_ha")
    if isinstance(enclosed, (int, float)) and not isinstance(enclosed, bool) and enclosed > 0:
        notes.append(ENCLOSED_NOTE.format(ha=enclosed))
    for item in made.get("diverted_openings") or []:
        if not isinstance(item, dict):
            continue
        opening = {
            "catchment_id": item.get("catchment_id"),
            "name": _data(str(item.get("name"))),
            "diverted_to": item.get("diverted_to"),
            "diverted_to_name": _data(str(item.get("diverted_to_name"))),
        }
        openings.append(opening)
        notes.append(OPENING_NOTE.format(name=opening["name"]))
    return notes, openings


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
    notes, openings = _boundary_notes(made)
    result.update(
        {
            "notes": notes,
            "enclosed_not_ticked_ha": made.get("enclosed_not_ticked_ha"),
            "diverted_openings": openings,
        }
    )
    d = doc.get("delineation") or {}
    unreviewed_delineation = bool(d.get("agent_authored") and not d.get("confirmed"))
    stretches = made.get("open_stretches") or []
    open_total = sum(x.get("length_m") or 0 for x in stretches if isinstance(x, dict))
    say = (
        f"Made boundary '{_data(str(boundary.get('title')))}' (input {boundary.get('id')}, {boundary.get('feature_count')} "
        f"features, {_m(made.get('perimeter_m'))} m round) from catchments {', '.join(used)}. Closed (Reflective) "
        f"except {len(stretches)} Transmissive stretch{'es' if len(stretches) != 1 else ''} at the drainage points, "
        f"{_m(open_total)} m in all. The boundary is unreviewed"
        + (", and so is the delineation" if unreviewed_delineation else "")
        + ": nothing is built or run until a person reviews it. To model with it: "
        f"update_scenario(boundary={boundary.get('id')}), then give the user the link."
    )
    parts = [say]
    if notes:
        parts.append("Tell the person: " + " ".join(notes))
    if _is_fast_screen(doc.get("shown_result")):
        parts.append("From a fast screen: tell the person a High resolution run may move the boundary.")
    ticks = _ticks_text(doc)
    if ticks:
        parts.append(ticks)
    words = result["warnings_in_words"]
    if words:
        parts.append("Warnings, relay every one: " + " ".join(f"({i}) {w}" for i, w in enumerate(words, start=1)))
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
            "enclosed_not_ticked_ha": made.get("enclosed_not_ticked_ha"),
            "diverted_openings": len(openings),
        },
        " ".join(parts),
        panel="catchments",
        line=delineation_id,
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Epic 4445 W6.2 (TASK-4469; D8, D16): adopt a confirmed line's catchments as a GeoNode layer
# ---------------------------------------------------------------------------
ADOPT_RULE = (
    "An adopted layer is a snapshot of a CONFIRMED line's catchments (name, chainage, area, where each drains) and the "
    "line itself, drawn as the Catchments panel drew them. It does not follow later changes; adopting again replaces "
    "its contents. Only a person confirms a line. A layer an agent adopts is unreviewed until a person adopts it again."
)


@_tool("adopt_catchments")
async def adopt_catchments(
    project_id: Annotated[int, "The project ID (Editor rights)"],
    delineation_id: Annotated[int, "`delineation_id` of a line a person has confirmed"],
    replace: Annotated[
        bool,
        "True only after the user said yes to replacing the contents of the line's adopted layer with the catchments as "
        "they are now (maps that show the layer then show the new catchments).",
    ] = False,
    authored_method: Annotated[
        str, "One short phrase saying why you are adopting these catchments. Shown to the person who reviews it."
    ] = "",
    sources: Annotated[list[dict] | None, SOURCES_ARGUMENT_HELP] = None,
) -> str:
    """Adopt a confirmed line's catchments as a GeoNode layer of the project: a snapshot to style and share.

    Refused unless a person has confirmed the line (ask them to, in the browser). When the line already has an adopted
    layer, the call is refused with that layer and its 'the line has changed since' mark: ask the user, and call again
    with replace=true only if they say yes. The layer you adopt is marked as made by an agent and unreviewed. The
    same mark is on get_delineation's `adopted_layer`. Give the user the link (`ui_url`): the layer is on the map in
    Adopted catchments.
    """
    cited = _clean_sources(sources)
    payload: dict = {"replace": bool(replace)}
    if authored_method.strip():
        payload["authored_method"] = authored_method.strip()
    if cited:
        payload["sources"] = cited
    base = f"/projects/{project_id}/delineations/{delineation_id}/"
    body, status = await client.post(f"{base}promote/", json=payload, raise_for_status=False)
    if status >= 500:
        raise ToolError(f"Hydrata API returned {status} for POST {base}promote/: {str(body)[:300]}")
    links = {"panel": "catchments", "line": delineation_id}
    reply = body if isinstance(body, dict) else {}
    if status >= 400:
        result = _adopt_words(_refused(body, status, "the adoption"))
        result["rule"] = ADOPT_RULE
        code = reply.get("error_code")
        if code == "NOT_CONFIRMED":
            say = (
                f"Adoption refused: {reply.get('detail')} Ask the user to check and confirm the line in the browser "
                "(the link), then adopt."
            )
        elif code == "PROMOTED_ALREADY":
            layer = _adopted_doc(reply.get("promoted_layer"))
            result["adopted_layer"] = layer
            changed = bool(layer and layer.get("line_changed_since"))
            say = (
                f"Already a layer{' (' + str(layer.get('title')) + ')' if layer else ''}; the line has "
                f"{'' if changed else 'not '}changed since. Adopting again replaces its contents and changes maps that "
                "show it. Ask the user; call adopt_catchments with replace=true only on a yes. Nothing changed."
            )
        else:
            say = result.pop("say_to_user")
        result.pop("say_to_user", None)
        await _hand_back(result, project_id, {"http_status": status, "error_code": code}, say, **links)
        return json.dumps(result, indent=2)
    layer = _adopted_doc(reply.get("promoted_layer")) or {}
    replaced = bool(reply.get("replaced"))
    result = {
        "outcome": "replaced" if replaced else "adopted",
        "http_status": status,
        "adopted_layer": layer,
        "replaced": replaced,
        "method": reply.get("method"),
        "vertices_before": reply.get("vertices_before"),
        "vertices_after": reply.get("vertices_after"),
        # TASK-4485: what of the line's sub-catchments the entry leaves out (a draft), the API's sentence, or null
        "left_out_sentence": reply.get("left_out_sentence"),
        "rule": ADOPT_RULE,
    }
    what = "replaced the contents of" if replaced else "made"
    say = (
        f"I {what} the layer {layer.get('title')} ({layer.get('alternate')}) from the {layer.get('feature_count')} "
        f"catchments of delineation {delineation_id}. It is on the map in "
        f"{layer.get('map_group') or 'Adopted catchments'}. It is a snapshot: it does not follow later changes."
    )
    carried = layer.get("sub_catchments") if isinstance(layer.get("sub_catchments"), dict) else None
    if carried:
        say += (
            f" It carries version {carried.get('version')} of the sub-catchments, as confirmed: "
            f"{carried.get('sub_catchments')} sub-catchments, {carried.get('nodes')} nodes and {carried.get('reaches')} "
            "reaches."
        )
    if reply.get("left_out_sentence"):
        say += f" {reply.get('left_out_sentence')}"
    if layer.get("unreviewed"):
        say += " It is unreviewed until you adopt it again yourself."
    summary = {
        "adopted_layer_id": layer.get("id"),
        "gn_layer": layer.get("gn_layer"),
        "alternate": layer.get("alternate"),
        "replaced": replaced,
        "unreviewed": layer.get("unreviewed"),
    }
    await _hand_back(result, project_id, summary, say, **links)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Epic 4445 W5.4 (TASK-4467, D16): an agent proposes a drainage correction; a person accepts it on the map
# ---------------------------------------------------------------------------
CORRECTION_WORDS = {
    "drainage_barrier": "drainage barrier",
    "drainage_opening": "drainage opening",
    "excluded_ground": "excluded ground",
}
PROPOSAL_RULE = (
    "An agent only proposes a drainage correction. Nothing changes until a person accepts it on the map. It never "
    "changes the terrain a flood model runs on."
)


@_tool("propose_drainage_correction")
async def propose_drainage_correction(
    project_id: Annotated[int, "The project ID (Editor rights)"],
    terrain_id: Annotated[int, "The terrain the correction belongs to (a delineation's `line.terrain_id`)"],
    kind: Annotated[
        Literal["drainage_barrier", "drainage_opening", "excluded_ground"],
        "drainage_barrier: a line water cannot cross (an embankment the terrain does not hold); drainage_opening: a "
        "line water passes through (a culvert or gap the terrain does not show), which can grow a catchment; "
        "excluded_ground: ground left out of the drainage analysis.",
    ],
    reason: Annotated[
        str,
        "One or two sentences the person reads before accepting: what you saw (for example the warning's sentence) and "
        "why this fixes it.",
    ],
    line: Annotated[
        list[list[float]] | None,
        "For a barrier or an opening: the line as [[lon, lat], ...] (at least 2 points, WGS84). For an opening, from "
        "where the water is to where it goes; for a barrier, across the way the water should not go.",
    ] = None,
    area: Annotated[
        list[list[float]] | None,
        "For excluded ground drawn by you: the ring as [[lon, lat], ...] (at least 3 points; it is closed for you).",
    ] = None,
    from_import_check: Annotated[
        bool, "For excluded ground: propose the extrapolated ground the terrain's import check marked (no `area`)."
    ] = False,
    delineation_id: Annotated[
        int | None,
        "The line the person should look at; the link opens it when it is on this terrain. Omit it and Hydrata picks a "
        "line on the terrain (the newest confirmed, else the newest).",
    ] = None,
) -> str:
    """Propose a drainage barrier, a drainage opening or excluded ground for a terrain. It is stored as a PROPOSAL and is
    NOT applied: a person accepts it on the map, and only then does the drainage analysis use it.

    Use it when a delineation warning names a fix (a `drainage_analysis_changed_ground` warning's `fix` is
    `drainage_barrier` or `drainage_opening`, with `fix_sentence`, `where` and the cut or fill `geometry`), or when you
    can say exactly where water really goes and the terrain does not show it. Read the ground first with terrain_profile. Tell the user the proposal is waiting and
    give them the link (`ui_url`): it opens a line on this terrain (`line_to_open`, named in the sentence) with the
    proposal. Lines on the terrain change only after the person accepts it and they run again.
    """
    words = CORRECTION_WORDS.get(kind)
    if words is None:
        raise ToolError("kind must be drainage_barrier, drainage_opening or excluded_ground. Nothing was sent.")
    why = reason.strip() if isinstance(reason, str) else ""
    if not why or len(why) > 200:
        raise ToolError("reason must be one or two sentences (1 to 200 characters) the person reads. Nothing was sent.")
    payload: dict = {"kind": kind, "note": why}
    if kind == "excluded_ground":
        if from_import_check:
            if area:
                raise ToolError("Give either from_import_check or area, not both. Nothing was sent.")
            payload["from_import_check"] = True
        else:
            ring = _lon_lat_pairs(area, "area", 3)
            if ring[0] != ring[-1]:
                ring.append(list(ring[0]))
            payload["geometry"] = {"type": "Polygon", "coordinates": [ring]}
    else:
        if area or from_import_check:
            raise ToolError(f"A {words} is a line: give `line`, not `area` or from_import_check. Nothing was sent.")
        payload["geometry"] = {"type": "LineString", "coordinates": _lon_lat_pairs(line, "line", 2)}
    if delineation_id is not None:
        payload["delineation_id"] = delineation_id
    path = f"/projects/{project_id}/terrain/{terrain_id}/drainage-corrections/"
    body, status = await client.post(path, json=payload, raise_for_status=False)
    if status >= 500:
        raise ToolError(f"Hydrata API returned {status} for POST {path}: {str(body)[:300]}")
    links = {"panel": "catchments", "line": delineation_id}
    if status >= 400:
        result = _refused(body, status, f"the {words} proposal")
        await _hand_back(result, project_id, {"http_status": status}, result.pop("say_to_user"), **links)
        return json.dumps(result, indent=2)
    answer = body if isinstance(body, dict) else {}
    proposal = answer.get("correction") or {}
    waiting = proposal.get("status") == "proposed"
    # TASK-4535 (finding F4): the API names a line on THIS terrain (the one asked for when it is on it, else the newest
    # confirmed, else the newest; null with no line). An older API names none: the line asked for, if any.
    picked = answer["line_to_open"] if "line_to_open" in answer else {"id": delineation_id}
    picked = picked if isinstance(picked, dict) and picked.get("id") is not None else None
    if picked and picked.get("name") is not None:
        picked = {"id": picked["id"], "name": _data(str(picked["name"]))}
    links = {
        "panel": "catchments",
        "line": (picked or {}).get("id"),
        "proposal": proposal.get("id") if waiting else None,
    }
    result = {
        "outcome": "proposed" if waiting else "stored",
        "status": "waiting" if waiting else proposal.get("status"),
        "http_status": status,
        "proposal": proposal,
        "proposals_waiting": sum(1 for c in answer.get("corrections") or [] if c.get("status") == "proposed"),
        "rule": PROPOSAL_RULE,
    }
    if waiting:
        result["line_to_open"] = picked
        note = _data(str(proposal.get("note") or why))
        if picked:
            named = f"line {picked['name']}" if picked.get("name") else f"delineation {picked['id']}"
            say = (
                f"I proposed a {words} on terrain {terrain_id} (proposal {proposal.get('id')}, {named}): {note} "
                "Open the link and Accept or Reject it under Notices, Agent. Nothing changes until you accept."
            )
        else:
            title = answer.get("terrain_title")
            terrain = f"the terrain {_data(str(title))}" if title else f"terrain {terrain_id}"
            say = (
                f"I proposed a {words} on terrain {terrain_id} (proposal {proposal.get('id')}): {note} "
                f"This terrain has no line: open Catchments and choose {terrain}, then Accept or Reject it under "
                "Notices, Agent. Nothing changes until you accept."
            )
    else:
        say = (
            f"Stored the {words} on terrain {terrain_id} (correction {proposal.get('id')}) without waiting for a person. "
            "Tell the user."
        )
    summary = {"proposal_id": proposal.get("id"), "kind": kind, "status": result["status"]}
    await _hand_back(result, project_id, summary, say, **links)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# get_stream_corridor (epic 4445 W12.2, TASK-4489; replaces TASK-4044): the streams of a terrain at a threshold the agent
# chooses, and the corridor round them, read from the terrain's KEPT drainage analysis. It starts nothing: no line run, no
# drainage analysis job, no conditioning, and it writes nothing.
# ---------------------------------------------------------------------------
STREAMS_LISTED = 20
CORRIDOR_NO_ANALYSIS_STEP = (
    "This terrain has no kept drainage analysis yet. A line run on it (delineate_catchments) makes one: ask the user "
    "before you start one, then read the streams again."
)


def _corridor_refusal(exc: HydrataAPIError) -> dict:
    """The API's refusal (a 400 or 409 whose body carries ``detail``), as a result the agent relays."""
    text = str(exc)
    body: dict = {}
    if " — " in text:
        try:
            parsed = json.loads(text.split(" — ", 1)[1])
            body = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            body = {}
    detail = body.get("detail") or text
    return {
        "outcome": "refused",
        "http_status": exc.status,
        "error_code": body.get("error_code") or exc.code,
        "detail": detail,
        "ran_anything": False,
    }


@_tool("get_stream_corridor")
async def get_stream_corridor(
    project_id: Annotated[int, "The project ID"],
    terrain_id: Annotated[int, "The terrain whose streams to read (a delineation's `line.terrain_id`, or get_terrain)"],
    threshold_ha: Annotated[
        float,
        "Where a stream starts: the drainage area in hectares that must drain through a cell for it to be a stream "
        "(0.1 to 1,000,000). Larger keeps only the bigger streams. Choose it with the user for what the model needs.",
    ],
    half_width_m: Annotated[
        float,
        "How far the corridor reaches each side of a stream, in metres (0 to 1000; default 40). 0 gives the streams "
        "with no corridor.",
    ] = 40.0,
    delineation_id: Annotated[
        int | None,
        "Keep the streams inside this line's catchments (a line on this terrain). Or give bbox; leave both out for the "
        "whole terrain.",
    ] = None,
    bbox: Annotated[
        list[float] | None,
        "Keep the streams inside this box: [west, south, east, north] in degrees (lon/lat).",
    ] = None,
    grid: Annotated[
        Literal["full", "quick"] | None,
        "'full' (high resolution) or 'quick' (the fast screen's coarser grid). Leave it out for the finest kept.",
    ] = None,
    include_stream_lines: Annotated[
        bool,
        "False (default): each stream as its drainage area and length only. True: with its line in lon/lat too.",
    ] = False,
) -> str:
    """Read the streams of a terrain and the corridor round them, from the terrain's kept drainage analysis.

    A stream is where at least `threshold_ha` of ground drains through. The corridor is the streams widened by
    `half_width_m` each side: a polygon to write as a mesh_region (write_input_layer) so the mesh is finer along the
    streams. Use it instead of working streams out yourself, and pass its `authored_method` when you write the corridor.
    Ask the user for the mesh region's resolution (a triangle edge length in metres, never finer than the terrain's
    `resolution_m`).

    It starts nothing (no line run, no drainage analysis, nothing written): a terrain with no kept drainage analysis is
    refused in words. Returns the corridor (a GeoJSON Feature in lon/lat), its area, the streams largest first (drainage
    area and length; their lines with include_stream_lines), the grid read, whether the drainage analysis is out of date
    (with the sentence to relay) and `authored_method`. Read-only.
    """
    if threshold_ha is None or not 0.1 <= float(threshold_ha) <= 1_000_000:
        raise ToolError("threshold_ha must be a number from 0.1 to 1,000,000 (hectares). Nothing was sent.")
    if half_width_m is None or not 0 <= float(half_width_m) <= 1000:
        raise ToolError("half_width_m must be a number from 0 to 1000 (metres). Nothing was sent.")
    params: dict = {"threshold_ha": float(threshold_ha), "half_width_m": float(half_width_m)}
    if bbox is not None:
        if delineation_id is not None:
            raise ToolError("Give delineation_id or bbox, not both. Nothing was sent.")
        ok = isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox)
        if not ok or not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90):
            raise ToolError(
                "bbox is [west, south, east, north] in degrees (lon/lat), west < east and south < north. "
                "Nothing was sent."
            )
        params["bbox"] = ",".join(f"{float(v):g}" for v in bbox)
    if delineation_id is not None:
        params["delineation_id"] = delineation_id
    if grid is not None:
        params["grid"] = grid
    links = {"panel": "catchments", "line": delineation_id}
    try:
        doc = await client.get(f"/projects/{project_id}/terrain/{terrain_id}/stream-corridor/", params=params)
    except HydrataAPIError as exc:
        if exc.status not in (400, 409):
            raise
        result = _corridor_refusal(exc)
        if result["error_code"] == "NO_DRAINAGE_ANALYSIS":
            result["next_step"] = CORRIDOR_NO_ANALYSIS_STEP
        say = f"Could not read the streams of terrain {terrain_id}: {result['detail']}"
        summary = {"terrain_id": terrain_id, "error_code": result["error_code"]}
        await _hand_back(result, project_id, summary, say, **links)
        return json.dumps(result, indent=2)
    doc = doc if isinstance(doc, dict) else {}
    features = ((doc.get("streams") or {}).get("features")) or []
    streams = []
    for feature in features[:STREAMS_LISTED] if not include_stream_lines else features:
        props = feature.get("properties") or {}
        item = {"drainage_area_ha": props.get("drainage_area_ha"), "length_m": props.get("length_m")}
        if include_stream_lines:
            item["geometry"] = feature.get("geometry")
        streams.append(item)
    corridor = doc.get("corridor")
    grid_read = doc.get("grid")
    resolution_m = doc.get("resolution_m")
    result = {
        "outcome": "ready",
        "terrain_id": doc.get("terrain_id", terrain_id),
        "terrain_title": _data(str(doc["terrain_title"])) if doc.get("terrain_title") is not None else None,
        "grid": grid_read,
        "resolution": HIGH_RES if grid_read == "full" else FAST_SCREEN if grid_read == "quick" else None,
        "resolution_m": resolution_m,
        "threshold_ha": doc.get("threshold_ha"),
        "kept_stream_threshold_ha": doc.get("kept_stream_threshold_ha"),
        "half_width_m": doc.get("half_width_m"),
        "area": doc.get("area"),
        "drainage_analysis": doc.get("drainage_analysis"),
        "read_from": doc.get("read_from"),
        "ran_anything": False,
        "stream_count": doc.get("stream_count", len(features)),
        "stream_length_m": doc.get("stream_length_m"),
        "longest_stream_m": doc.get("longest_stream_m"),
        "largest_drainage_area_ha": doc.get("largest_drainage_area_ha"),
        "streams": streams,
        "corridor": corridor,
        "corridor_area_ha": doc.get("corridor_area_ha"),
        "authored_method": doc.get("authored_method"),
    }
    if not include_stream_lines and len(features) > STREAMS_LISTED:
        result["streams_listed"] = f"The {STREAMS_LISTED} largest of {len(features)}."
    if corridor:
        finest = f"{resolution_m:g} m" if isinstance(resolution_m, (int, float)) else "the terrain's cell size"
        result["next_step"] = (
            "To make the mesh finer along these streams, ask the user for the resolution (a triangle edge length in "
            f"metres, never finer than {finest}), then write_input_layer(project_id={project_id}, kind='mesh_region', "
            "features=[the corridor with properties {'resolution': <their figure>, 'resolution_units': 'm'}], "
            "authored_method=<authored_method>)."
        )
    count = result["stream_count"]
    threshold = doc.get("threshold_ha", threshold_ha)
    parts = [
        f"{count} stream{'s' if count != 1 else ''} draining at least {threshold:g} ha on terrain {terrain_id} "
        f"({result['resolution'] or grid_read}, {resolution_m:g} m)."
        if isinstance(resolution_m, (int, float))
        else f"{count} streams draining at least {threshold:g} ha on terrain {terrain_id}."
    ]
    if corridor:
        parts.append(
            f"The corridor {doc.get('half_width_m', half_width_m):g} m each side covers {doc.get('corridor_area_ha')} ha."
        )
    if doc.get("sentence"):
        parts.append(str(doc["sentence"]))
    summary = {
        "terrain_id": terrain_id,
        "threshold_ha": threshold,
        "streams": count,
        "corridor_area_ha": doc.get("corridor_area_ha"),
        "drainage_analysis": (doc.get("drainage_analysis") or {}).get("state"),
    }
    await _hand_back(result, project_id, summary, " ".join(parts), **links)
    return json.dumps(result, indent=2)
