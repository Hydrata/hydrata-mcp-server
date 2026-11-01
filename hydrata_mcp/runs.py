"""Run tools: start, poll, read, cancel, retry and list simulation runs."""

import json
from http import HTTPStatus
from typing import Annotated

from ._common import _tool, client
from ._helpers import (
    INPUT_REVIEW_REFUSALS,
    _elide_presigned,
    _hand_back,
    _input_review_hand_back,
    _money,
    _poll_until,
    _post_result,
    _project_base_map,
    _run_quote_words,
    _status_of,
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

    Needs a launch-level agent key (a run spends compute); a read or draft key
    gets 403 AGENT_LEVEL_INSUFFICIENT — the user creates a launch key on the
    site's Agent keys page (/agents/).

    The scenario must be in 'built' status. Returns 202 with the new run.
    The run transitions through: built → queued → computing → processing → complete.

    After starting, poll get_run_status to track progress. Returns 409
    if the scenario is not in the correct state. The returned run record has
    its presigned `s3_*_url` links elided (6-hour capabilities).

    INPUT REVIEW (Hydrata enforces it): while an input you wrote is not reviewed by
    the user, the run is not started - `outcome` `unreviewed_inputs`,
    `error_code` UNREVIEWED_INPUTS, a `ui_url` to the scenario's review screen.
    A run built from inputs other than the ones reviewed now answers
    `outcome` `run_invalidated` (RUN_INVALIDATED): build_scenario with
    rebuild=true, then start it again.

    HOW THE RUN IS PAID (TASK-4101): a started run's answer carries `quote` (what the
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
        "Open the link to watch it; the results appear on the map when it completes.",
        run=run_id,
    )
    return json.dumps(result, indent=2)


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
            f"The user gave consent{price}: run {run_id} has started ({run_status}). Open the link to watch "
            "it; the results appear on the map when it completes.",
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
    emails them when it finishes.

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

    data, polls, elapsed, timed_out = await _poll_until(fetch, settled, wait, RUN_WAIT_POLL_INTERVAL_SECONDS)
    if isinstance(data, dict):
        data["say_to_user"] = _run_status_line(data)
        if wait:
            data["waited_seconds"] = elapsed
            data["timed_out"] = timed_out
            if data.get("status") not in RUN_TERMINAL_STATUSES:
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
        message = record.get("error_message")
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


async def _sww_summary(run_id, run) -> dict:
    """`{state, size_bytes, ui_url}` for a run's full time series (TASK-4062).

    Only those keys are copied from the API answer, so a download link could
    never ride along. Best effort: a failed lookup gives `state: null` and a
    short `error`, never a failed get_run.
    """
    summary: dict = {"state": None, "size_bytes": None, "ui_url": None}
    try:
        body = await client.get(f"/runs/{run_id}/sww/")
        if isinstance(body, dict):
            summary["state"] = body.get("state")
            summary["size_bytes"] = body.get("size_bytes")
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
    (available, not_kept, archived or restoring), `size_bytes`, and a `ui_url`
    to the Results panel where the person downloads it (or requests a restore
    of an archived one and is emailed when it is ready). The download link
    itself is never returned.
    """
    data = _elide_presigned(await client.get(f"/runs/{run_id}/"))
    if isinstance(data, dict):
        data["sww"] = await _sww_summary(run_id, data)
    return json.dumps(data, indent=2)


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
