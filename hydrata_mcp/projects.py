"""Project tools: list, read and create projects."""

import json
from typing import Annotated

from ._common import _data, _tool, client
from ._helpers import _elide_presigned, _ui_url


# ---------------------------------------------------------------------------
# Tool 1: list_projects
# ---------------------------------------------------------------------------
@_tool("list_projects")
async def list_projects(
    page: Annotated[int, "Page number (default 1)"] = 1,
    page_size: Annotated[int, "Results per page, max 100 (default 100)"] = 100,
) -> str:
    """List ANUGA simulation projects accessible to the authenticated user.

    Returns a paginated list of projects with their names, projections,
    and base map references.
    """
    data = await client.get("/projects/", params={"page": page, "page_size": page_size})
    return json.dumps(_elide_presigned(data), indent=2)


# ---------------------------------------------------------------------------
# Tool 2: get_project
# ---------------------------------------------------------------------------
@_tool("get_project")
async def get_project(
    project_id: Annotated[int, "The project ID"],
) -> str:
    """Get one ANUGA project: id, name, projection, visibility, your role, its base map — and the link to it.

    Returns the project record (`id`, `name`, `projection` (EPSG code),
    `simple_view_config`, `visibility`, `owner_username`, `my_role`,
    `base_map`) plus `ui_url`: the link that opens the project's map, for the
    user (`<site>/catalogue/#/map/<base_map>`; null while a new project's map
    is still being created). Scenarios are NOT listed here — use get_scenario
    with a scenario id.
    """
    data = await client.get(f"/projects/{project_id}/")
    if isinstance(data, dict):
        data = dict(data, ui_url=_ui_url(data.get("base_map")))
        data["levels_datum"] = levels = data.get("levels_datum") if isinstance(data.get("levels_datum"), dict) else None
        say = _levels_sentence(levels)
        mismatch = await _levels_mismatch(project_id)
        if mismatch:
            say += f" WARNING: {_data(mismatch)} Tell the user, and do not mix the two sets of heights."
        data["say_to_user"] = say
    return json.dumps(_elide_presigned(data), indent=2)


def _levels_sentence(levels) -> str:
    """TASK-4118: the project's levels datum in one sentence for the user."""
    if not levels:
        return "No levels datum is recorded for this project: ask the user which datum their levels are in."
    who = f", declared by {_data(str(levels['declared_by']))}" if levels.get("declared_by") else ""
    return f"The user's levels are on {_data(str(levels.get('label') or levels.get('datum')))}{who}."


async def _levels_mismatch(project_id: int) -> str | None:
    """The newest terrain's levels-datum mismatch sentence, or None (best effort: a failed read says nothing)."""
    try:
        rows = await client.get(f"/projects/{project_id}/terrain/")
        rows = rows if isinstance(rows, list) else (rows or {}).get("results", [])
        newest = max(rows, key=lambda row: row.get("id", 0)) if rows else None
        text = newest.get("levels_datum_mismatch") if isinstance(newest, dict) else None
        return text if isinstance(text, str) and text else None
    except Exception:
        return None
