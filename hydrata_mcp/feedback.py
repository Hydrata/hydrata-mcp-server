"""Feedback tools: tell the Hydrata team about a capability the platform does not have (TASK-3964, epic 3958 D7)."""

import json
from typing import Annotated

from ._common import _tool, client

SUGGESTIONS_PATH = "/api/v2/agents/suggestions/"
PASSED_ON = "I've passed this request to the Hydrata team."


@_tool("suggest_capability")
async def suggest_capability(
    what: Annotated[str, "What the person asked for that no tool can do, in one or two plain sentences"],
    goal: Annotated[str, "What they were trying to achieve with it (optional)"] = "",
) -> str:
    """Tell the Hydrata team about something the person asked for that NO existing tool can do.

    Use it only when you have checked the other tools and none can do what the person asked
    (never to report a bug, and never instead of a tool that can do it). Describe the need, not a
    workaround; send no personal data, keys or file contents. It changes none of the person's data.
    The reply tells you what to say: relay `say_to_user` to the person as it is. Limit: 20 a day
    per person.
    """
    payload = {"what": what}
    if goal:
        payload["goal"] = goal
    body, _status = await client.post_from_origin(SUGGESTIONS_PATH, json=payload)
    result = {
        "outcome": "passed_on",
        "id": body.get("id") if isinstance(body, dict) else None,
        "say_to_user": PASSED_ON,
    }
    return json.dumps(result, indent=2)
