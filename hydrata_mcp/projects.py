"""Project tools: list, read and create projects."""

import json
from typing import Annotated

from ._common import _tool, client
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
    return json.dumps(_elide_presigned(data), indent=2)
