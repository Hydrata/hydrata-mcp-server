"""Helpers shared by more than one tool family: POST results, presigned-URL elision, polling, hand-back links and input-review relays."""

import asyncio
import json
import re
import time
from urllib.parse import quote, urlencode

from ._common import _data, client, config


def _post_result(body, status_code: int) -> str:
    """Tool text for a POST: the API body plus `http_status`.

    Some endpoints answer 202 (queued) rather than 200/201, and the agent
    needs to see which; a non-dict body is wrapped as {"response": body}
    so the status key always has a dict to live in.
    """
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# TASK-3187 (W0, epic 3200) — presigned-URL elision.
#
# A run record carries five `s3_*_url` keys, each a presigned S3 URL: a 6-hour
# capability (`X-Amz-Expires=21600`) carrying an STS token. A scenario detail
# carries TEN of them — `latest_run` and `latest_complete_run` are both full
# run dicts — and the read tools relayed every one of them verbatim into a
# transcript the client persists. That widens what a leaked transcript is worth
# beyond what the caller asked for, against decision D7 (epic 2467): the agent
# moves bytes only through URLs it explicitly asked for.
#
# Applied PER TOOL and NEVER inside `_post_result`: that helper is shared with
# presign_terrain_upload, whose signed `upload_url` IS what the caller asked
# for (the sole documented exception), plus finalize_terrain_upload,
# create_project, create_time_series, attach_input_layer, cancel_run and
# retry_run.
#
# Two rules, applied RECURSIVELY so a key the API adds tomorrow is covered by
# default (the six read tools' bodies are nested: list_runs is
# `{count, results: [...]}`, get_terrain composes its own envelope):
#   * KEY NAME — an `s3_*_url` key's string value is replaced outright.
#   * VALUE SHAPE — any string carrying `X-Amz-Signature` or
#     `X-Amz-Security-Token` has the signed URL REDACTED IN PLACE. In place,
#     because a run's `log` is ~17 KB of build output (clean today, but one
#     future line quoting a signed URL would otherwise silently nuke get_run's
#     whole log). The entire whitespace-delimited token goes, not just the
#     matched parameter: a presigned URL carries SIX `X-Amz-*` params, so
#     redacting only the signature would leave `X-Amz-Credential=…` behind.
# ---------------------------------------------------------------------------
ELIDED_PRESIGNED = "[presigned-url-elided]"
# Detection is the two capability-bearing params ONLY — an unreviewed S3 link is
# just a link, and `bbox_stats_url` / a layer's `catalogURL` must survive.
PRESIGNED_MARKERS = ("X-Amz-Signature", "X-Amz-Security-Token")
# The whole non-whitespace run around any `X-Amz-*` param. Greedy, so one
# substitution swallows scheme, host, key and every parameter of the URL.
PRESIGNED_URL_TOKEN = re.compile(r"\S*X-Amz-\S*")


def _is_s3_url_key(key) -> bool:
    """`s3_package_url`, `s3_depth_max_url`, … — the key-name rule."""
    return isinstance(key, str) and key.startswith("s3_") and key.endswith("_url")


def _elide_presigned(value):
    """Recursively return `value` with every presigned S3 URL elided.

    Returns a new structure; the caller's body is never mutated. Non-string
    values under an `s3_*_url` key (a `null` package URL on a run that has no
    package yet) are left alone — "not there" is information the agent needs.
    """
    if isinstance(value, dict):
        return {
            key: (
                ELIDED_PRESIGNED if _is_s3_url_key(key) and isinstance(item, str) and item else _elide_presigned(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_elide_presigned(item) for item in value]
    if isinstance(value, str) and any(marker in value for marker in PRESIGNED_MARKERS):
        return PRESIGNED_URL_TOKEN.sub(ELIDED_PRESIGNED, value)
    return value


# ---------------------------------------------------------------------------
# Bounded polling — ONE loop for every tool that waits on the platform
# (get_terrain, attach_input_layer, build_scenario).
#
# Why bounded, and why 180/200 (TASK-4063): prod's nginx proxies /mcp/ with a 300 s
# proxy_read_timeout (geonode-https.j2). A tool call cut by the proxy returns
# NOTHING to the agent, so a call that stops early, reports `timed_out` with
# the last status and lets the agent re-call is strictly better than a longer
# one. Values above the ceiling are clamped rather than refused.
# ---------------------------------------------------------------------------
POLL_DEFAULT_TIMEOUT_SECONDS = 180
POLL_MAX_TIMEOUT_SECONDS = 200


def _status_of(record) -> str | None:
    """`status` of an API record, or None when it is not a dict."""
    return record.get("status") if isinstance(record, dict) else None


async def _poll_until(fetch, is_terminal, timeout_seconds, poll_interval_seconds):
    """Call `fetch()` until `is_terminal(value)` or `timeout_seconds` elapses.

    Returns (last value, polls, elapsed_seconds, timed_out). Always fetches at
    least once (timeout 0 = one read, no wait); a terminal value on the last
    read wins over the bound. The sleep is min(interval, remaining) — the W1a
    round-1 sweep's Tier-A finding: an uncapped interval longer than what was
    left overshot the bound (and prod's 300 s proxy cut).
    """
    timeout_seconds = max(0, min(int(timeout_seconds), POLL_MAX_TIMEOUT_SECONDS))
    poll_interval_seconds = max(0.0, float(poll_interval_seconds))
    started = time.monotonic()
    polls = 0
    while True:
        value = await fetch()
        polls += 1
        if is_terminal(value):
            timed_out = False
            break
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            timed_out = True
            break
        await asyncio.sleep(min(poll_interval_seconds, remaining))
    return value, polls, round(time.monotonic() - started, 1), timed_out


# ---------------------------------------------------------------------------
# TASK-3203 (W1, epic 3200) — the hand-back.
#
# Every tool that CHANGES something answers with three extra keys, so the
# agent can hand the engineer the link and the numbers instead of a bare "done":
#   * ui_url      — `<ui origin>/catalogue/#/map/<base_map>?panel=…&layer=…&
#                   scenario=…&run=…`; gmc's handBackParamsEpic (TASK-3205)
#                   reads the hash query, opens the panel and frames the layer.
#   * summary     — a dict of human-checkable numbers (feature count, bbox,
#                   elevation range, triangle estimate, price).
#   * say_to_user — the sentence the agent should relay.
# Composing them is BEST EFFORT and never fails the tool: the change already
# happened, so a lookup error leaves `ui_url` null and says so in
# `hand_back_error` rather than hiding the result.
# ---------------------------------------------------------------------------
HAND_BACK_PARAM_ORDER = ("panel", "layer", "scenario", "run", "kind")


def _ui_origin() -> str:
    """Where a HUMAN opens the link — never the loopback API URL (see Config.ui_origin)."""
    if config.ui_origin:
        return config.ui_origin
    if config.api_host:
        return f"https://{config.api_host}"
    return client._origin


def _ui_url(base_map, **params) -> str | None:
    """`<origin>/catalogue/#/map/<base_map>[?panel=…&layer=…&scenario=…&run=…]`, or None."""
    if base_map in (None, ""):
        return None
    query = [(key, params[key]) for key in HAND_BACK_PARAM_ORDER if params.get(key) not in (None, "")]
    url = f"{_ui_origin()}/catalogue/#/map/{base_map}"
    # ':' stays literal so `layer=geonode:bdy_1_boundary_01` reads as the alternate.
    return f"{url}?{urlencode(query, quote_via=quote, safe=':')}" if query else url


def _alternate(name) -> str | None:
    """A GeoNode layer name as its `workspace:name` alternate (gmc resolves layers by it)."""
    if not isinstance(name, str) or not name:
        return None
    return name if ":" in name else f"geonode:{name}"


async def _project_base_map(project_id) -> int | None:
    """The project's base map id (TASK-3201 put it on the V2 project record)."""
    data = await client.get(f"/projects/{project_id}/")
    return data.get("base_map") if isinstance(data, dict) else None


async def _hand_back(result: dict, project_id, summary: dict, say_to_user: str, **params) -> dict:
    """Add ui_url / summary / say_to_user to `result` (best effort, never raises)."""
    ui_url = None
    try:
        if project_id is not None:
            ui_url = _ui_url(await _project_base_map(project_id), **params)
    except Exception as exc:  # the change already landed; never hide it behind a link lookup
        result["hand_back_error"] = f"could not compose ui_url: {exc}"
    if ui_url is None:
        say_to_user = f"{say_to_user} (No map link could be composed for project {project_id}.)"
    result["ui_url"] = ui_url
    result["summary"] = summary
    result["say_to_user"] = say_to_user
    return result


async def _layer_summary(alternate) -> dict:
    """bbox_wgs84 of a published vector layer, from GeoNode's datasets API.

    Best effort: a failure is a null plus a `bbox_error` string. No feature
    count: that needed a WFS hits request through /gs/, which Hydrata refuses
    for every agent key (TASK-3728), so it was always null (TASK-3749). The
    caller reports `has_features` from the input row the API returns instead.
    """
    summary: dict = {"layer": alternate, "bbox_wgs84": None}
    if not alternate:
        return summary
    try:
        listing = await client.get_from_origin("/api/v2/datasets/", params={"filter{alternate}": alternate})
        datasets = listing.get("datasets") if isinstance(listing, dict) else None
        polygon = (datasets[0].get("ll_bbox_polygon") or {}) if datasets else {}
        ring = (polygon.get("coordinates") or [[]])[0]
        if ring:
            xs, ys = [pt[0] for pt in ring], [pt[1] for pt in ring]
            summary["bbox_wgs84"] = [min(xs), min(ys), max(xs), max(ys)]
    except Exception as exc:
        summary["bbox_error"] = str(exc)
    return summary


# TASK-3208 (epic 3200 W2b; absorbs 3209) — input review. Hydrata refuses a
# build or a run while an input an agent key wrote is unreviewed (409
# UNREVIEWED_INPUTS), and a run built from inputs other than the ones reviewed
# now (409 RUN_INVALIDATED). Only the person reviews, in the browser, on the
# scenario's review screen (`?panel=input-review`). Relayed as a normal result keyed
# on error_code; this server never waits for the person inside a call — the
# agent polls get_scenario's `unreviewed_inputs`.
UNREVIEWED_INPUTS = "UNREVIEWED_INPUTS"
RUN_INVALIDATED = "RUN_INVALIDATED"
INPUT_REVIEW_REFUSALS = (UNREVIEWED_INPUTS, RUN_INVALIDATED)


def _input_names(items) -> str:
    names = []
    for item in items or []:
        if isinstance(item, dict):
            title = _data(item.get("title")) or f"{item.get('kind')} {item.get('layer_id')}"
            names.append(f"{title} ({item.get('kind')})")
    return ", ".join(names) or "the inputs I wrote"


async def _input_review_hand_back(result: dict, project_id, scenario_id) -> dict:
    """outcome / ui_url / say_to_user / next_step for an UNREVIEWED_INPUTS or RUN_INVALIDATED refusal."""
    if result.get("error_code") == UNREVIEWED_INPUTS:
        unreviewed = result.get("unreviewed_inputs") or []
        result["outcome"] = "unreviewed_inputs"
        result["next_step"] = (
            "Give the user ui_url: only they can review these inputs, in their browser. Do not call "
            "build_scenario or start_simulation in a loop: call get_scenario every 30-60 s and, once its "
            "unreviewed_inputs is empty, call it again."
        )
        say = (
            f"I wrote these inputs, so you need to look at them and review them before anything is built "
            f"or run: {_input_names(unreviewed)}. Open the link (the scenario's review screen), look at each "
            "one on the map and review them. Nothing was built or started."
        )
        summary = {"scenario_id": scenario_id, "outcome": "unreviewed_inputs", "unreviewed_count": len(unreviewed)}
        params = {"panel": "input-review", "scenario": scenario_id}
    else:
        changed = result.get("changed_inputs") or []
        result["outcome"] = "run_invalidated"
        result["next_step"] = (
            f"Call build_scenario(project_id, scenario_id={scenario_id}, rebuild=true), then start_simulation."
        )
        say = (
            f"The built model is out of date: {', '.join(changed) or 'its inputs'} changed and were reviewed "
            "after it was built, so I need to build it again before it can run. Nothing was started."
        )
        summary = {"scenario_id": scenario_id, "outcome": "run_invalidated", "changed_inputs": changed}
        params = {"panel": "scenarios", "scenario": scenario_id}
    return await _hand_back(result, project_id, summary, say, **params)
