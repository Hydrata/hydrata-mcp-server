"""Calibration tools: compare a run with flood observations, exclusions and tolerance."""

import json
from typing import Annotated

from fastmcp.exceptions import ToolError

from ._common import _data, _tool, client
from ._helpers import _elide_presigned, _hand_back
from .runs import _run_project_and_scenario

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
    bias -0.12 m · RMSE 0.35 m · 1 excluded', then any dry points on their own
    part. A sentence (an older server) or None passes through."""
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
    # TASK-3983: dry points are out of n / bias / RMSE (TASK-3981), worded as
    # gn_anuga.calibration_stats.headline_text does
    dry = headline.get("dry_points")
    if isinstance(dry, dict):
        line = f"Dry points: {int(dry.get('wet') or 0)} of {int(dry.get('n') or 0)} wet in the model"
        if dry.get("worst_depth_m") is not None:
            line += f" ({dry.get('worst_name') or 'deepest'} {dry['worst_depth_m']:.2f} m)"
        parts.append(line)
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
    timing error, NSE), `datum_checks` (warn the user only when `warning` is true:
    both datums known and different; `not_recorded` = the terrain has no
    recorded datum, stated in `note`, not a warning)
    and one row per observation: observed_m / modelled_m / residual_m (modelled -
    observed, + = model high), within, scored / not_scored_reason, match
    (`outside_run_window` = not this flood), excluded + exclusion {reason, by, at}.
    `sampling` counts how the modelled values were read: `centroid` (the peak
    of the mesh triangle the observation sits in), `nearest_node_fallback` (an
    older run without that peak: the nearest mesh node's value, tell the user)
    and `nearest_node` (just outside the mesh); each row's `sample.method` says
    which.
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
        result = {
            "run": run_id,
            "has_comparison": False,
            "error_code": "NO_COMPARISON",
            "detail": body.get("detail"),
            "http_status": status_code,
        }
        await _hand_back(
            result,
            project_id,
            {"run_id": run_id, "has_comparison": False},
            f"Run {run_id}: {NO_COMPARISON_WORDS}. A design storm run is not compared with flood observations.",
            panel="scenarios",
            scenario=scenario_id,
            run=run_id,
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
        result["history"] = (
            await client.get(_comparison_path(run_id, "history/"), params={"layer": layer_id, "fid": fid})
        ).get("history")
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
        say += (
            f" {row.get('subtype_label') or 'Observation'} {_data(row.get('name')) or fid}: {outcome}"
            + (" (excluded)" if row.get("excluded") else "")
            + "."
        )
    # TASK-3850: warn only when both datums are known and differ (`warning`);
    # a terrain with no recorded datum is stated, not warned about.
    checks = [check for check in result.get("datum_checks") or [] if isinstance(check, dict)]
    warnings = [
        check.get("message")
        for check in checks
        if (check.get("warning") or check.get("status") == "mismatch") and check.get("message")
    ]
    if warnings:
        say += " Datum warning: " + " ".join(warnings)
    notes = sorted({check.get("note") for check in checks if check.get("note") and not check.get("warning")})
    if notes:
        say += " " + " ".join(notes)
    # TASK-3843: an older run's store has no per-triangle peak; say so.
    fallback = (result.get("sampling") or {}).get("nearest_node_fallback") or 0
    if fallback:
        say += (
            f" This is an older run: {fallback} observation{'s' if fallback != 1 else ''} "
            "read the nearest mesh node's value, not the peak of the triangle it sits in."
        )
    await _hand_back(
        result,
        project_id,
        {
            "run_id": run_id,
            "headline": result.get("headline"),
            "tolerance_m": result.get("tolerance_m"),
            "row_count": len(rows),
        },
        say + " Open the link to see the run in the map.",
        panel="scenarios",
        scenario=scenario_id,
        run=run_id,
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
        run_id,
        "exclusions/",
        {"layer": layer_id, "fid": fid, "excluded": excluded, "reason": reason},
        f"set_observation_exclusion on run {run_id}",
    )
    project_id, scenario_id = await _run_project_and_scenario(run_id, None)
    if result.get("excluded"):
        say = (
            f"Flood observation {fid} is excluded from run {run_id}'s comparison "
            f"(reason: {_data(result.get('reason') or reason)})."
        )
    else:
        say = f"Flood observation {fid} is included again in run {run_id}'s comparison."
    if result.get("headline_after"):
        say += f" Now: {result['headline_after']}."
    await _hand_back(
        result,
        project_id,
        {
            "run_id": run_id,
            "layer_id": layer_id,
            "fid": fid,
            "excluded": result.get("excluded"),
            "reason": result.get("reason"),
        },
        say,
        panel="scenarios",
        scenario=scenario_id,
        run=run_id,
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
        f"/projects/{project_id}/comparison-tolerance/", json={"tolerance_m": tolerance_m}
    )
    result = {**(body if isinstance(body, dict) else {"response": body}), "http_status": status_code}
    saved = result.get("tolerance_m", tolerance_m)
    await _hand_back(
        result,
        project_id,
        {"project_id": project_id, "tolerance_m": saved},
        f"Project {project_id}'s default comparison tolerance is now ±{float(saved):.2f} m.",
        panel="scenarios",
    )
    return json.dumps(result, indent=2)
