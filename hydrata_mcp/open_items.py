"""TASK-4592 (epic 4445 W12.6 fix round): what waits for a person, listed for an agent.

list_open_items - the open items of a project, or of one line (`line_id`): the warnings (advice: they never hold a
                  confirm), what an agent built (it waits for a person's one click, Accept) and an agent's proposals
                  (nothing changes until a person accepts them). It reads the API's engineering decisions (GET
                  engineering-decisions/, `?line_id=`) and decides nothing.

dismiss_warning - TASK-4666 (Decision 953 A): dismisses ONE warning, by the subject_type, subject_id and fingerprint
                  list_open_items gives it, through POST engineering-decisions/ with the agent key. Hydrata records it as
                  the agent's, under the person's grant (Decision 945). Nothing else is decided by a tool.

Engineering decisions (Decisions 943-948): accepting or rejecting what an agent made is a person's act in the browser only;
Hydrata refuses an agent key that tries (403 AGENT_KEY_SCOPE). Only a warning may be dismissed by an agent, under the
person's grant. Words (Decision 889): sub-catchments, create, divide, Adopt.
"""

import json
import re
from typing import Annotated

from fastmcp.exceptions import ToolError

from ._common import _data, _tool, client
from ._helpers import _hand_back

KINDS = ("warning", "agent_built", "proposal")
WHO_DECIDES = {
    "warning": (
        "A warning is advice: it never holds a confirm. The person dismisses it on the map, or leaves it. An agent may "
        "dismiss it too (dismiss_warning) under the person's grant, recorded as the agent's."
    ),
    "agent_built": "Built by an agent: it waits until a person accepts it on the map with one click.",
    "proposal": "An agent's proposal: nothing changes until a person accepts it on the map.",
}
OPEN_ITEMS_RULE = (
    "Relay these to the user with the link. Warnings are advice and never hold a confirm. What an agent built and an "
    "agent's proposals wait for a person's one click on the map. No tool accepts or rejects them, and Hydrata refuses "
    "an agent key that tries (AGENT_KEY_SCOPE). An agent may dismiss a warning it has checked (dismiss_warning, with the "
    "warning's subject_type, subject_id and fingerprint), under the person's grant."
)
WARNING_SUBJECTS = (
    "delineation",
    "sub_catchments",
)  # what the API dismisses: a warning on a line, or on sub-catchments
FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _row(item: dict) -> dict:
    kind = item.get("kind")
    row = {
        "kind": kind,
        "subject_type": item.get("subject_type"),
        "subject_id": item.get("subject_id"),
        "label": _data(str(item.get("label") or "")),
        "who_decides": WHO_DECIDES.get(kind, WHO_DECIDES["proposal"]),
    }
    if kind == "warning" and item.get("fingerprint"):
        row["fingerprint"] = item[
            "fingerprint"
        ]  # what dismiss_warning needs; only a warning can be dismissed by a tool
    return row


def _say(project_id, line_id, counts: dict) -> str:
    total = sum(counts.values())
    where = f"on line {line_id}" if line_id is not None else f"in project {project_id}"
    if not total:
        return f"Nothing waits for a person {where}."
    parts = []
    if counts["warning"]:
        parts.append(
            f"{_plural(counts['warning'], 'warning', 'warnings')} (advice: "
            f"{'it never holds' if counts['warning'] == 1 else 'they never hold'} a confirm)"
        )
    if counts["agent_built"]:
        parts.append(_plural(counts["agent_built"], "thing an agent built", "things an agent built"))
    if counts["proposal"]:
        parts.append(_plural(counts["proposal"], "proposal", "proposals"))
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    waiting = counts["agent_built"] + counts["proposal"]
    tail = ""
    if waiting:
        tail = (
            ", each waiting for your one click on the map" if waiting > 1 else ", waiting for your one click on the map"
        )
    return f"{_plural(total, 'item waits', 'items wait')} for a person {where}: {listed}{tail}. Open the link."


@_tool("list_open_items")
async def list_open_items(
    project_id: Annotated[int, "The project ID"],
    line_id: Annotated[
        int | None,
        "Only the items on this line (its `delineation_id`): its warnings, the warnings and proposals of its "
        "sub-catchments, what an agent built on it, and the proposed drainage corrections on its terrain. Omit it for "
        "the whole project.",
    ] = None,
) -> str:
    """List what waits for a person's judgement in a project, or on one line: the warnings, what an agent built (a line,
    a diversion it ticked, sub-catchments or an edit, an adopt, a model boundary) and an agent's proposals (a drainage
    correction, a boundary or link on sub-catchments). Each item has its `kind` (warning, agent_built or proposal), its
    `label` and `who_decides`; `counts` per kind; `decided_count` items already decided. Warnings are advice: they never
    hold a confirm, and an agent may dismiss one it has checked (dismiss_warning, with the warning's `subject_type`,
    `subject_id` and `fingerprint` from here) under the person's grant. What an agent built and an agent's proposals wait
    for a person's one click on the map: no tool accepts or rejects them, and Hydrata refuses an agent key that tries.
    Give the user the link. Read-only.
    """
    params = {"line_id": line_id} if line_id is not None else None
    doc = await client.get(f"/projects/{project_id}/engineering-decisions/", params=params)
    doc = doc if isinstance(doc, dict) else {}
    found = [i for i in doc.get("open") or [] if isinstance(i, dict)]
    rows = [_row(i) for i in found]
    counts = {kind: sum(1 for r in rows if r["kind"] == kind) for kind in KINDS}
    result = {
        "project_id": project_id,
        "line_id": line_id,
        "count": len(rows),
        "counts": counts,
        "open": rows,
        "decided_count": len([i for i in doc.get("decided") or [] if isinstance(i, dict)]),
        "rule": OPEN_ITEMS_RULE,
    }
    await _hand_back(result, project_id, counts, _say(project_id, line_id, counts), panel="catchments", line=line_id)
    return json.dumps(result, indent=2)


NOT_A_WARNING = (
    "Only a warning is dismissed here. What an agent built and an agent's proposals are not warnings: a person accepts or "
    "rejects them on the map with one click, and no tool does. Give the user the link. Nothing was sent."
)


@_tool("dismiss_warning")
async def dismiss_warning(
    project_id: Annotated[int, "The project ID"],
    subject_type: Annotated[
        str, "The warning's `subject_type` from list_open_items: 'delineation' (a line) or 'sub_catchments'"
    ],
    subject_id: Annotated[
        int, "The warning's `subject_id` from list_open_items (the line, or the set of sub-catchments)"
    ],
    fingerprint: Annotated[str, "The warning's `fingerprint` from list_open_items (64 hex characters)"],
) -> str:
    """Dismiss ONE warning you have checked, under the person's grant. A warning is advice: it never holds a confirm, and
    dismissing it changes nothing on the map. Hydrata records the dismissal as the agent's (credential agent key, with
    the grant), and the person sees it in the project's history and can take it back on the map. Read the warning first
    with list_open_items and pass its `subject_type`, `subject_id` and `fingerprint` unchanged; if the warning changed
    since, Hydrata says so (CHANGED): read it again. Only a warning: what an agent built and an agent's proposals are
    refused here without calling Hydrata, because a person accepts or rejects them on the map. Say to the user which
    warning you dismissed and why you judged it safe.
    """
    if subject_type not in WARNING_SUBJECTS:
        raise ToolError(NOT_A_WARNING)
    if not isinstance(fingerprint, str) or not FINGERPRINT.match(fingerprint):
        raise ToolError(
            "fingerprint is the warning's fingerprint exactly as list_open_items gives it (64 hex characters). "
            "Nothing was sent."
        )
    payload = {
        "subject_type": subject_type,
        "subject_id": subject_id,
        "fingerprint": fingerprint,
        "decision": "dismissed",
    }
    body, status = await client.post(
        f"/projects/{project_id}/engineering-decisions/", json=payload, raise_for_status=False
    )
    if status >= 500:
        raise ToolError(f"Hydrata API returned {status} for POST engineering-decisions/: {str(body)[:300]}")
    doc = body if isinstance(body, dict) else {}
    line = subject_id if subject_type == "delineation" else None
    if status >= 400:
        sentence = doc.get("detail") or doc.get("error") or doc.get("error_code") or f"HTTP {status}"
        result = {
            "outcome": "refused",
            "http_status": status,
            "error_code": doc.get("error_code"),
            "detail": _data(str(sentence)),
        }
        say = f"Refused dismissing the warning: {_data(str(sentence))} Nothing changed."
        await _hand_back(
            result,
            project_id,
            {"http_status": status, "error_code": result["error_code"]},
            say,
            panel="catchments",
            line=line,
        )
        return json.dumps(result, indent=2)
    decision = doc.get("decision") if isinstance(doc.get("decision"), dict) else {}
    again = status == 200
    result = {
        "outcome": "already_dismissed" if again else "dismissed",
        "http_status": status,
        "decision_id": decision.get("id"),
        "credential": decision.get("credential"),
        "subject_type": subject_type,
        "subject_id": subject_id,
    }
    say = (
        "That warning was already dismissed. Nothing changed."
        if again
        else "Dismissed the warning under the person's grant. It is recorded as yours (the agent's) in the project's "
        "history, and the person can take it back on the map."
    )
    await _hand_back(result, project_id, {"decision_id": result["decision_id"]}, say, panel="catchments", line=line)
    return json.dumps(result, indent=2)
