"""Run tools: start, poll, read, cancel, retry and list simulation runs."""

import asyncio
import json
from http import HTTPStatus
from typing import Annotated

from ._common import _data, _tool, client
from ._helpers import (
    INPUT_REVIEW_REFUSALS,
    TOKEN_REFRESH_NEXT_STEP,
    _elide_presigned,
    _hand_back,
    _input_review_hand_back,
    _money,
    _poll_until,
    _post_result,
    _project_base_map,
    _run_quote_words,
    _status_of,
    _token_cap_spent,
    _ui_origin,
    _ui_url,
)
from .client import HydrataAPIError


# ---------------------------------------------------------------------------
# Tool 4: start_simulation
# ---------------------------------------------------------------------------
@_tool("start_simulation")
async def start_simulation(
    scenario_id: Annotated[int, "The scenario ID to run"],
    consent_timeout_seconds: Annotated[
        int,
        "When the run needs the person's consent (CONSENT_REQUIRED), how long this "
        "call keeps polling the run for their click before it returns (ceiling 200). "
        "Default 0: return the consent link at once, so you can give it to them — "
        "they cannot press a button they have not been shown.",
    ] = 0,
    poll_interval_seconds: Annotated[float, "Seconds between consent polls (default 5)"] = 5.0,
) -> str:
    """Start a flood simulation run for a built scenario.

    WAIT FOR THE GO-AHEAD: this spends compute, so before you call it give the
    person the ui_url and WAIT for their explicit go-ahead to start the run;
    reviewed inputs are not a go-ahead, and never start a run on your own say-so.

    Needs a launch-level agent key (a run spends compute); a read or draft key
    gets 403 AGENT_LEVEL_INSUFFICIENT — the user creates a launch key on the
    site's Agent keys page (/agents/).

    The scenario must be in 'built' status. Returns 202 with the new run.
    The run transitions through: built → queued → computing → processing → complete.

    After starting, tell the person how they will hear about the end (`say_to_user` says it:
    you cannot watch the run after your reply; Hydrata emails them when it completes or
    fails). If the run is estimated at under about 10 minutes, keep following it with
    get_run_status(wait_seconds=180) and report the completion yourself in the same turn.
    Poll get_run_status to track progress. Returns 409
    if the scenario is not in the correct state. The returned run record has
    its presigned `s3_*_url` links elided (6-hour capabilities).

    INPUT REVIEW (Hydrata enforces it): while an input you wrote is not reviewed by
    the user, the run is not started - `outcome` `unreviewed_inputs`,
    `error_code` UNREVIEWED_INPUTS, a `ui_url` to the scenario's review screen.
    A run built from inputs other than the ones reviewed now answers
    `outcome` `run_invalidated` (RUN_INVALIDATED): build_scenario with
    rebuild=true, then start it again.

    HOW THE RUN IS PAID: a started run's answer carries `quote` (what the
    user is charged; `0.00` inside the daily free band), `quote_list_price` (the run's
    real price, the same figure the map and billing show) and `paid_by` (`free_band` or
    `credit`), and `say_to_user` states them. Say that figure, never another: the only
    price you can quote is `quote` / `quote_list_price` (built run) or `quote_estimate`
    (before a build, always called an estimate).

    SPEND CONSENT (Hydrata enforces it; you cannot): a run whose quote does not
    fit your agent key's monthly spend limit (default $0), or that Hydrata
    cannot price, is NOT started. The result is then a normal (non-error)
    answer with `outcome` `consent_required`, `error_code` CONSENT_REQUIRED,
    `quoted_cost_usd` and `consent_url`: give the user the link and the price
    (`say_to_user`). Only they can approve, in their browser. Then do NOT call
    start_simulation again for this run: poll get_run_status(run_id) — the run
    leaves `built` once they press the button. (With consent_timeout_seconds > 0
    this call polls the run itself and answers `outcome` `consented`, with the
    run, if they approve within that time.)
    """
    body, status_code = await client.post(
        f"/scenarios/{scenario_id}/run/",
        json={},
        raise_for_status=False,
    )
    if status_code == 409 and isinstance(body, dict) and body.get("error_code") == CONSENT_REQUIRED:
        return json.dumps(
            await _await_consent(scenario_id, body, consent_timeout_seconds, poll_interval_seconds), indent=2
        )
    if status_code == 409 and isinstance(body, dict) and body.get("error_code") in INPUT_REVIEW_REFUSALS:
        result = dict(body, http_status=status_code)
        return json.dumps(await _input_review_hand_back(result, body.get("project_id"), scenario_id), indent=2)
    if status_code >= 400:
        raise HydrataAPIError(
            _api_refusal_message(status_code, "POST", f"/scenarios/{scenario_id}/run/", body),
            status=status_code,
            code=body.get("error_code") if isinstance(body, dict) else None,
        )
    # TASK-3187 — the 202 relays RunSerializerV2 for a run that is already
    # `built`, so it ALREADY holds the signed package URL: elide here, never in
    # `_post_result` (presign_terrain_upload shares it and must keep its URL).
    result = json.loads(_post_result(_elide_presigned(body), status_code))
    run_id = result.get("id")
    # TASK-4101 (D24/D34): say how the run is paid and the quote, the SAME figure as the pane chip and the
    # billing record. Hydrata starts a run (202) only when it fits the free band (quote 0.00, the real price
    # is quote_list_price) or is charged to the person's credit (quote > 0); a run it cannot start is a
    # refusal above, never this answer.
    quote, list_price = result.get("quote"), result.get("quote_list_price")
    paid_by = "unknown" if _money(quote) is None else ("free_band" if _money(quote) == 0 else "credit")
    paid_words = {
        "free_band": "it is free, inside the daily free band",
        "credit": "it is charged to the user's compute credit",
        "unknown": "its price is not available",
    }[paid_by]
    await _hand_back(
        result,
        result.get("project"),
        {
            "run_id": run_id,
            "scenario_id": scenario_id,
            "status": result.get("status"),
            "compute_backend": result.get("compute_backend"),
            "quote": quote,
            "quote_list_price": list_price,
            "paid_by": paid_by,
        },
        f"Run {run_id} has started for scenario {scenario_id} ({result.get('status')}): {paid_words}; "
        f"the run's price is {_run_quote_words(quote, list_price)}. "
        f"{CANNOT_WATCH_WORDS} Open the link to watch it live; the results appear on the map when it completes.",
        run=run_id,
    )
    return json.dumps(result, indent=2)


# TASK-4105 (D36): a connector agent cannot act after its reply, so the start message says how the person hears.
CANNOT_WATCH_WORDS = (
    "I can't watch it after this reply: Hydrata will email you when it is done, or ask me to check on it."
)

# TASK-3214 (W2a, epic 3200, D27) — run consent. Hydrata (not this server)
# decides whether an agent may spend: over the key's monthly limit, or for a
# run it cannot price, the dispatch is 409 CONSENT_REQUIRED + a consent link
# the PERSON opens in their browser. This replaces in-chat (MRTR) consent,
# which only works in interactive Claude Code. The relay is a non-error
# result, as build_scenario's refusals are; the link goes on the UI origin.
CONSENT_REQUIRED = "CONSENT_REQUIRED"
RUN_AWAITING_CONSENT = "built"


def _api_refusal_message(status_code: int, method: str, path: str, body) -> str:
    """The same message client._client_error_message gives a raised 4xx (status, reason, body)."""
    try:
        reason = HTTPStatus(status_code).phrase
    except ValueError:
        reason = ""
    message = f"Hydrata API returned {status_code}: {reason} for {method} {path}"
    detail = json.dumps(body, separators=(",", ":"), ensure_ascii=False)[:1000] if body else ""
    return f"{message} — {detail}" if detail else message


async def _await_consent(scenario_id, refusal: dict, timeout_seconds, poll_interval_seconds) -> dict:
    """Relay a CONSENT_REQUIRED and wait (bounded) for the person to consent in the browser."""
    run_id = refusal.get("run_id")
    path = refusal.get("consent_path")
    consent_url = f"{_ui_origin()}{path}" if path else refusal.get("consent_url")
    record = None
    polls, elapsed, timed_out = 0, 0.0, True
    if run_id is not None:

        async def fetch():
            return await client.get(f"/runs/{run_id}/status/")

        record, polls, elapsed, timed_out = await _poll_until(
            fetch,
            lambda value: _status_of(value) not in (None, RUN_AWAITING_CONSENT),
            timeout_seconds,
            poll_interval_seconds,
        )
    run_status = _status_of(record)
    cost = refusal.get("quoted_cost_usd")
    if run_status not in (None, RUN_AWAITING_CONSENT):
        result = {
            "outcome": "consented",
            "id": run_id,
            "run_id": run_id,
            "scenario_id": scenario_id,
            "status": run_status,
            "quoted_cost_usd": cost,
            "consent_url": consent_url,
            "polls": polls,
            "elapsed_seconds": elapsed,
        }
        project_id, _scenario = await _run_project_and_scenario(run_id, None)
        price = f" for ${cost}" if cost is not None else ""
        await _hand_back(
            result,
            project_id,
            {"run_id": run_id, "scenario_id": scenario_id, "status": run_status, "quoted_cost_usd": cost},
            f"The user gave consent{price}: run {run_id} has started ({run_status}). {CANNOT_WATCH_WORDS} "
            "Open the link to watch it live; the results appear on the map when it completes.",
            run=run_id,
        )
        return result
    if cost is None:
        say = (
            f"Hydrata could not price this run in advance, so you need to start it yourself: open {consent_url} "
            "in your browser (signed in to Hydrata) and press the button. Nothing has started."
        )
    else:
        say = (
            f"This run is quoted at ${cost}, more than this agent key may spend without asking you "
            f"(${refusal.get('month_to_date_usd')} of its ${refusal.get('monthly_cap_usd')} monthly limit "
            f"used). To start it, open {consent_url} in your browser (signed in to Hydrata) and press the "
            "button. Nothing has started."
        )
    return {
        "outcome": "consent_required",
        "error_code": CONSENT_REQUIRED,
        "state": refusal.get("state"),
        "http_status": 409,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "consent_url": consent_url,
        "quoted_cost_usd": cost,
        "mesh_triangle_count": refusal.get("mesh_triangle_count"),
        "compute_target": refusal.get("compute_target"),
        "monthly_cap_usd": refusal.get("monthly_cap_usd"),
        "month_to_date_usd": refusal.get("month_to_date_usd"),
        "detail": refusal.get("detail"),
        "timed_out": timed_out,
        "polls": polls,
        "elapsed_seconds": elapsed,
        "say_to_user": say,
        "next_step": (
            "Give the user consent_url and the price. Only they can approve it, in their browser. Do "
            "not call start_simulation again for this run: poll get_run_status(run_id) — it leaves "
            "'built' once they approve."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 5: get_run_status
# ---------------------------------------------------------------------------
RUN_WAIT_MAX_SECONDS = 180  # TASK-4063's proven ceiling (nginx /mcp/ 300 s; a 180 s call survived claude.ai)
RUN_TERMINAL_STATUSES = ("complete", "error", "cancelled")
RUN_WAIT_PROGRESS_POINTS = 5.0  # a long-poll returns early when progress moves this far
RUN_WAIT_POLL_INTERVAL_SECONDS = 5.0


def _run_moved(first: dict, now: dict) -> bool:
    """True when status, phase or progress (by >= 5 points) differs from the first read."""
    if now.get("status") != first.get("status") or now.get("phase") != first.get("phase"):
        return True
    a, b = first.get("progress_pct"), now.get("progress_pct")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(b - a) >= RUN_WAIT_PROGRESS_POINTS
    return False


@_tool("get_run_status")
async def get_run_status(
    run_id: Annotated[int, "The run ID"],
    wait_seconds: Annotated[
        int,
        "Wait up to this many seconds for the run to move before answering (max 180, "
        "the proven ceiling; larger values are clamped). Returns early when the status, "
        "the phase or the progress (by 5 points) changes. Default 0: answer at once.",
    ] = 0,
) -> str:
    """Status of a simulation run, optionally waiting for it to move.

    Use this instead of get_run. Returns: id, status, `phase`, status_detail,
    progress_pct (0-100), eta_seconds, error_message, compute_backend, and
    `say_to_user`.

    FOLLOWING A RUN FROM CHAT: call with wait_seconds=180. The call returns as soon
    as the status, phase or progress (5 points) changes, or after 180 s, whichever is
    first. Then RELAY `say_to_user` to the person (one short line after every call) and call
    again. Do not sleep or wait yourself between calls: the wait happens inside this
    call. After about 10 minutes of this in one turn, stop and tell the person to ask
    you for the status: you cannot watch the run between their messages, and Hydrata
    emails them when it finishes. If the answer has `token_refresh_pending: true`, your sign-in
    is being refreshed: do not call again at once, follow `next_step`.

    `phase` is never empty: it is `status`, except `preparing_inputs` while a
    `computing` run has no progress yet (it is downloading its input package
    and building the domain — minutes on a big model; this is NOT a stall).
    Terminal states: complete, error, cancelled. A waiting call returns at once
    on a terminal state.
    """

    async def fetch():
        record = await client.get(f"/runs/{run_id}/status/")
        if isinstance(record, dict):
            record = dict(record, phase=_run_phase(record))
        return record

    wait = max(0, min(int(wait_seconds), RUN_WAIT_MAX_SECONDS))
    first: dict = {}

    def settled(record) -> bool:
        if not isinstance(record, dict):
            return True
        if not first:
            first.update(record)
        if record.get("status") in RUN_TERMINAL_STATUSES:
            return True
        return _run_moved(first, record)

    cap_spent = bool(wait) and _token_cap_spent()  # TASK-4223: read BEFORE the poll (the poll takes time)
    data, polls, elapsed, timed_out = await _poll_until(fetch, settled, wait, RUN_WAIT_POLL_INTERVAL_SECONDS)
    if isinstance(data, dict):
        data["say_to_user"] = _run_status_line(data)
        if wait:
            data["waited_seconds"] = elapsed
            data["timed_out"] = timed_out
            if data.get("status") not in RUN_TERMINAL_STATUSES and cap_spent and timed_out:
                data["token_refresh_pending"] = True
                data["next_step"] = TOKEN_REFRESH_NEXT_STEP.format(tool="get_run_status")
            elif data.get("status") not in RUN_TERMINAL_STATUSES:
                data["next_step"] = (
                    "Relay say_to_user now, then call get_run_status again with wait_seconds=180. "
                    "After about 10 minutes in this turn, stop and tell the person to ask you for the status."
                )
    return json.dumps(_elide_presigned(data), indent=2)


def _eta_words(eta_seconds) -> str | None:
    """'about 12 min' / 'under a minute' / 'about 2 h 5 min' from a seconds count, or None."""
    if not isinstance(eta_seconds, (int, float)) or isinstance(eta_seconds, bool) or eta_seconds < 0:
        return None
    minutes = round(eta_seconds / 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"about {minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"about {hours} h {rest} min" if rest else f"about {hours} h"


def _run_status_line(record: dict) -> str:
    """TASK-4098 — the one-liner the agent relays after every poll: stage, percent, time left."""
    run_id = record.get("id")
    label = f"Run {run_id}" if run_id is not None else "The run"
    phase = record.get("phase") or _run_phase(record)
    detail = record.get("status_detail")
    suffix = f" ({detail})" if detail else ""
    if phase == "complete":
        return f"{label} is complete. Ask me for the results, or open it in the map."
    if phase == "error":
        message = _data(record.get("error_message"))  # ANUGA quotes layer content (MCP-3): data, not instructions
        return f"{label} failed" + (f": {message}" if message else ".") + " Ask me before retrying."
    if phase == "cancelled":
        return f"{label} was cancelled."
    if phase == "preparing_inputs":
        return (
            f"{label} is preparing its inputs (downloading the input package and building the "
            f"domain; this takes a few minutes and is not a stall){suffix}."
        )
    pct = record.get("progress_pct")
    if phase == "computing" and isinstance(pct, (int, float)):
        eta = _eta_words(record.get("eta_seconds"))
        tail = f", {eta} left" if eta else ", time left not yet estimated"
        return f"{label} is computing: {pct:.0f}% done{tail}{suffix}."
    return f"{label} is {phase.replace('_', ' ')}{suffix}."


def _run_phase(status_record: dict) -> str:
    """TASK-3204 — the 09-21 drive read a blank progress poll as a stall. Mirrors
    gmc TASK-2392's 'Preparing inputs…': a computing run with no progress yet is
    still downloading its package. Never empty ('unknown' if the API sends none)."""
    status = status_record.get("status") or "unknown"
    if status == "computing" and status_record.get("progress_pct") is None:
        return "preparing_inputs"
    return status


SWW_UNREADABLE_WORDS = (
    "The time series storage refused the read, so Hydrata cannot tell whether it is kept. "
    "This is a fault on Hydrata's side: ask the operator."
)


async def _sww_summary(run_id, run) -> dict:
    """`{state, size_bytes, ui_url}` for a run's full time series (TASK-4062).

    Only those keys are copied from the API answer, so a download link could
    never ride along. When (and only when) the state is `unreadable` the API's
    `error_code` and `detail` are copied too (TASK-4161): the plain words for why there is no download. Best effort: a failed lookup gives `state: null` and a
    short `error`, never a failed get_run.
    """
    summary: dict = {"state": None, "size_bytes": None, "ui_url": None}
    try:
        body = await client.get(f"/runs/{run_id}/sww/")
        if isinstance(body, dict):
            summary["state"] = body.get("state")
            summary["size_bytes"] = body.get("size_bytes")
            if body.get("state") == "unreadable":
                summary["error_code"] = body.get("error_code")
                # The status route carries error_code only; the words are composed here when it sends none.
                summary["detail"] = body.get("detail") or SWW_UNREADABLE_WORDS
    except Exception as exc:
        summary["error"] = f"could not check the time series: {exc}"
        return summary
    try:
        base_map = await _project_base_map(run.get("project"))
        summary["ui_url"] = _ui_url(base_map, panel="results", scenario=run.get("scenario"))
    except Exception:
        pass
    return summary


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

    `sww` reports the run's full time series (the raw ANUGA output): `state`
    (available, not_kept, archived, restoring or unreadable), `size_bytes`, and a `ui_url`
    to the Results panel where the person downloads it (or requests a restore
    of an archived one and is emailed when it is ready). The download link
    itself is never returned. `unreadable` means the storage refused the read:
    there is no download to offer, so tell the person and ask them to ask the
    operator; `sww.error_code` and `sww.detail` carry the reason in words.

    CULVERTS (a complete run): `culverts` lists the flow the model moved THROUGH each
    culvert's barrel: `name`, `feature_id`, `peak_q_m3s` and `peak_q_time_s` (seconds
    into the run), `volume_m3` (net volume through the barrel; null when the run kept
    no series), `warnings` (e.g. a mesh too coarse at the culvert), `has_timeseries`.
    `culvert_sign_convention` gives the sign once: positive flow runs from the
    culvert's Inlet to its Outlet. Relay `culverts_note`. This is the culvert's
    flow; get_flow_hydrograph on a line drawn along a culvert measures the flow
    ACROSS that line, a different number, so quote `culverts` for a culvert.
    `culverts: []` means the run modelled no culvert (say so; never invent a
    number). When the culvert results cannot be read, `culverts` is absent and
    `culverts_note` says so; the rest of the run record is still good.
    """
    data = _elide_presigned(await client.get(f"/runs/{run_id}/"))
    if isinstance(data, dict):
        data["sww"] = await _sww_summary(run_id, data)
        if data.get("status") == "complete":
            data.update(await _culvert_summary(run_id))
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# TASK-4282 (epic 4117 W8, D68): the flow through each culvert's barrel, from hydrata's EXISTING
# GET runs/<id>/structure-results/ (StructureResultSerializerV2) and .../<row id>/timeseries/.
# ---------------------------------------------------------------------------
CULVERT_KIND = "culvert"
CULVERT_SIGN_RULE = "Positive flow runs from the culvert's Inlet to its Outlet."
CULVERT_SERIES_READS_MAX = 20  # a volume costs one series read per culvert; past this many, volume_m3 is null
NOT_THE_BARREL_WORDS = (
    "If this line follows a culvert, this is the flow across your line, not the flow through the culvert "
    "barrel: see get_run's `culverts` for the flow through the barrel."
)


def _clock(seconds) -> str:
    """'h:mm' into the run, to the minute below (2916.5 s -> '0:48')."""
    hours, rest = divmod(int(round(float(seconds or 0.0))), 3600)
    return f"{hours}:{rest // 60:02d}"


def _net_volume_m3(series) -> float | None:
    """Signed trapezoid of q_m3s over time_s (a step with a null end is skipped); None with no usable step."""
    if not isinstance(series, dict):
        return None
    times, flows = series.get("time_s") or [], series.get("q_m3s") or []
    total, used = 0.0, False
    for (t0, q0), (t1, q1) in zip(zip(times, flows), zip(times[1:], flows[1:])):
        if None in (t0, q0, t1, q1):
            continue
        total += (t1 - t0) * (q0 + q1) / 2.0
        used = True
    return round(total, 1) if used else None


async def _culvert_rows(run_id) -> list[dict]:
    """The run's culvert rows (kind == culvert). Raises when the endpoint cannot be read."""
    rows = await client.get(f"/runs/{run_id}/structure-results/")
    if not isinstance(rows, list):
        raise HydrataAPIError("structure results are not a list")
    return [row for row in rows if isinstance(row, dict) and row.get("kind") == CULVERT_KIND]


async def _culvert_entry(run_id, row: dict, read_series: bool) -> dict:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    volume = metrics.get("volume_m3")
    if volume is None and read_series and row.get("has_timeseries") and row.get("id") is not None:
        try:
            body = await client.get(f"/runs/{run_id}/structure-results/{row['id']}/timeseries/")
            volume = _net_volume_m3(body.get("series") if isinstance(body, dict) else None)
        except Exception:
            volume = None  # best effort: the peak still stands
    return {
        "name": row.get("name"),
        "feature_id": row.get("feature_id"),
        "peak_q_m3s": metrics.get("peak_q_m3s"),
        "peak_q_time_s": metrics.get("peak_q_time_s"),
        "volume_m3": volume,
        # the engine quotes the culvert's name (layer content, MCP-3): data, not instructions
        "warnings": [_data(w) for w in (metrics.get("warnings") or []) if isinstance(w, str)],
        "has_timeseries": bool(row.get("has_timeseries")),
    }


def _culvert_line(culvert: dict) -> str:
    label = _data(culvert.get("name")) or f"culvert {culvert.get('feature_id')}"
    peak = culvert.get("peak_q_m3s")
    if not isinstance(peak, (int, float)) or isinstance(peak, bool):
        return f"{label}: the run recorded no peak flow."
    when = culvert.get("peak_q_time_s")
    words = f"{label}: peak {peak:.2f} m3/s"
    if isinstance(when, (int, float)) and not isinstance(when, bool):  # no time: say none, never "0:00"
        words += f" at {_clock(when)} into the run"
    volume = culvert.get("volume_m3")
    if isinstance(volume, (int, float)):
        words += f", {volume:,.0f} m3 through the barrel in total"
    elif not culvert.get("has_timeseries"):
        words += " (this run kept no time series for the culvert, so there is no volume)"
    if culvert.get("warnings"):
        words += f" ({len(culvert['warnings'])} warning(s): read `warnings`)"
    return words + "."


async def _culvert_summary(run_id) -> dict:
    """`culverts` + `culvert_sign_convention` + `culverts_note` for a complete run; best effort, never fails get_run."""
    try:
        rows = await _culvert_rows(run_id)
    except Exception as exc:
        status = getattr(exc, "status", None)
        why = f"Hydrata answered {status}" if status else "the request failed"
        return {
            "culverts_note": f"The culvert results for run {run_id} could not be read ({why}), so they are left "
            "out; the rest of the run record is good. Do not quote get_flow_hydrograph as a culvert's flow."
        }
    if not rows:
        return {
            "culverts": [],
            "culverts_note": f"Run {run_id} modelled no culverts, so there is no flow through a culvert to report.",
        }
    culverts = list(
        await asyncio.gather(*(_culvert_entry(run_id, row, i < CULVERT_SERIES_READS_MAX) for i, row in enumerate(rows)))
    )
    note = (
        "Flow through each culvert's barrel, as the model computed it. "
        + " ".join(_culvert_line(c) for c in culverts)
        + f" {CULVERT_SIGN_RULE} This is not the flow across a line drawn along the culvert (get_flow_hydrograph)."
    )
    return {"culverts": culverts, "culvert_sign_convention": CULVERT_SIGN_RULE, "culverts_note": note}


async def _run_has_culverts(run_id) -> bool | None:
    """True/False when the run's culvert rows can be read; None when they cannot."""
    try:
        return bool(await _culvert_rows(run_id))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# TASK-4032: get_flow_hydrograph
# ---------------------------------------------------------------------------
FLOW_HYDROGRAPH_CLIENT_TIMEOUT_S = 75.0  # the backend budget (30 s) covers the discharge read only (MCP-4)


def _flow_say(data: dict) -> str:
    """One or two sentences for the person: peak, time, volume, sign, mass balance."""
    clock = _clock(data.get("peak_time_s"))
    # TASK-4203 (ex-4196): a one-frame store has no spacing (null): never "(to the nearest 0 s frame)".
    spacing = data.get("frame_spacing_s")
    frame = f"{spacing:.0f} s frame" if isinstance(spacing, (int, float)) and spacing > 0 else "saved frame"
    first = (
        f"Across that line the flow peaks at {data.get('peak_m3s', 0.0):.2f} m3/s at {clock} into the run "
        f"(to the nearest {frame}), and {data.get('volume_m3', 0.0):,.0f} m3 "
        f"crosses in total. {data.get('sign_convention', '')}"
    )
    mb = data.get("mass_balance") or {}
    if mb.get("rain_in_m3") is None:
        second = f"Mass balance: {mb.get('left_in_model_m3', 0.0):,.0f} m3 is left in the model; rain and inflow could not be read."
    else:
        second = (
            f"Mass balance: {mb['rain_in_m3']:,.0f} m3 of rain and {mb.get('inflow_in_m3') or 0:,.0f} m3 of inflow "
            f"went in, {mb.get('left_in_model_m3', 0.0):,.0f} m3 is left in the model, so about "
            f"{mb.get('out_by_difference_m3') or 0:,.0f} m3 left through the model's open edges (rain + inflow "
            "minus the water left in the model; it is not measured at the edge)."
        )
    # Full review 03-11-26 (MCP-5): a dry line, a peak on the first or last frame and a coarsened spacing change
    # what the numbers mean; the per-frame caveat is implicit in "to the nearest frame". TASK-4187: dropped by
    # prefix, the plain-word caveat AND the old one (a hydrata not yet shipped), so neither is relayed twice.
    peak_caveat = ("Results are saved", "the peak is resolved to the nearest frame")
    notes = [
        w.strip().rstrip(".")
        for w in (data.get("warnings") or [])
        if isinstance(w, str) and w.strip() and not w.startswith(peak_caveat)
    ]
    third = (" Note: " + "; ".join(notes) + ".") if notes else ""
    return f"{first} {second}{third}"


@_tool("get_flow_hydrograph")
async def get_flow_hydrograph(
    run_id: Annotated[int, "The run ID"],
    line: Annotated[
        list[list[float]],
        "The line to measure across, as [[lon, lat], [lon, lat], ...] in WGS84 (at least 2 points), "
        "in the order drawn. Positive flow crosses from the line's left bank to its right bank.",
    ],
) -> str:
    """Flow (Q) across a drawn line, at every frame of a completed run.

    Integrates depth times velocity normal to the line from the run's playback store:
    returns `time_s` and `q_m3s` per frame, `peak_m3s` and `peak_time_s`, `volume_m3`,
    `sign_convention`, `frame_spacing_s`, `warnings` (the peak is resolved to the nearest
    frame, so the true peak may fall between frames) and a `mass_balance` (rain and inflow
    in, left in the model; outflow is by difference, open-boundary flux is not integrated).
    Relay `say_to_user`. This is the model's flow, not a gauge reading.

    A run with no playback store answers outcome `no_playback_store`; a bad line answers
    `bad_line` with the reason.
    """
    # Full review 03-11-26 (MCP-4): the backend's 30 s budget covers the discharge read only; the store open and
    # the mass balance sit outside it, so the client waits longer than the budget, and a 504
    # FLOW_HYDROGRAPH_TIMEOUT is handed back with its words instead of becoming a generic server error.
    body, status_code = await client.post(
        f"/runs/{run_id}/flow-hydrograph/",
        json={"line": line},
        raise_for_status=False,
        relay_statuses=(HTTPStatus.GATEWAY_TIMEOUT,),
        timeout=FLOW_HYDROGRAPH_CLIENT_TIMEOUT_S,
    )
    code = body.get("error_code") if isinstance(body, dict) else None
    if status_code == HTTPStatus.NOT_FOUND and code == "NO_PLAYBACK_STORE":
        return json.dumps(
            {
                "outcome": "no_playback_store",
                "run_id": run_id,
                "say_to_user": f"Run {run_id} has no playback store, so the flow across a line cannot be read "
                "(it may not have finished, or it predates playback stores).",
            },
            indent=2,
        )
    if status_code == HTTPStatus.NOT_FOUND:
        # MCP-8: a run that does not exist, or that this key cannot read, is not "no playback store".
        raise HydrataAPIError(f"Run {run_id} was not found, or you cannot read it.", status=status_code, code=code)
    if status_code == HTTPStatus.GATEWAY_TIMEOUT:
        detail = body.get("detail") if isinstance(body, dict) else None
        return json.dumps(
            {
                "outcome": "too_slow",
                "run_id": run_id,
                "detail": detail,
                "say_to_user": (detail or "The flow hydrograph took longer than its time budget.")
                + " Draw a shorter line, or one that crosses fewer cells, and ask again; do not repeat the same line.",
            },
            indent=2,
        )
    if status_code == HTTPStatus.BAD_REQUEST:
        reason = (body.get("detail") or body.get("error")) if isinstance(body, dict) else None
        return json.dumps(
            {
                "outcome": "bad_line",
                "run_id": run_id,
                "error": reason,
                "say_to_user": f"That line cannot be used: {reason or 'it is not a [[lon, lat], ...] line'}.",
            },
            indent=2,
        )
    if status_code >= 400 or not isinstance(body, dict):
        raise HydrataAPIError(str(body)[:300], status=status_code)
    body["say_to_user"] = _flow_say(body)
    # TASK-4282: whether THIS line follows a culvert is not known cheaply (it would need the scenario's culvert
    # geometry); a run that modelled a culvert (or whose culverts cannot be read) gets the conditional sentence.
    if await _run_has_culverts(run_id) is not False:
        body["say_to_user"] += f" {NOT_THE_BARREL_WORDS}"
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 7: cancel_run
# ---------------------------------------------------------------------------
@_tool("cancel_run")
async def cancel_run(
    run_id: Annotated[int, "The run ID to cancel"],
) -> str:
    """Cancel an in-flight simulation run.

    Needs a launch-level agent key; a read or draft key gets 403
    AGENT_LEVEL_INSUFFICIENT.

    Works on runs in built, queued, or computing status. Cleans up
    compute resources (terminates EC2 instance, Celery task, or Batch job).

    Returns 409 if the run is already in a terminal state
    (complete, cancelled, or error).
    """
    body, status_code = await client.post(f"/runs/{run_id}/cancel/")
    result = json.loads(_post_result(body, status_code))
    project_id, scenario_id = await _run_project_and_scenario(run_id, result)
    await _hand_back(
        result,
        project_id,
        {"run_id": run_id, "scenario_id": scenario_id, "status": result.get("status")},
        f"Run {run_id} is cancelled ({result.get('status')}); its compute has been released. "
        "Open the link to see the scenario.",
        panel="scenarios",
        scenario=scenario_id,
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

    Needs a launch-level agent key (a retry rebuilds the package and can start
    AWS Batch); a read or draft key gets 403 AGENT_LEVEL_INSUFFICIENT.

    Starts a fresh package build of the run's scenario as a NEW run, which
    becomes the scenario's latest run. The errored run keeps its status and
    error as history, and the response describes that old run; the new run's
    ID is get_scenario's `run_id`. Only valid when status is 'error'. Returns
    409 for any other state.
    """
    body, status_code = await client.post(f"/runs/{run_id}/retry/")
    result = json.loads(_post_result(body, status_code))
    project_id, scenario_id = await _run_project_and_scenario(run_id, result)
    await _hand_back(
        result,
        project_id,
        {
            "run_id": run_id,
            "scenario_id": scenario_id,
            "status": result.get("status"),
            "rebuilding": result.get("rebuilding"),
        },
        f"Run {run_id} is being retried: scenario {scenario_id} is rebuilding as a NEW run. "
        "Open the link to follow the build.",
        panel="scenarios",
        scenario=scenario_id,
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
