"""TASK-4053 (epic 4051 W1.2) — attach is ONE path for every kind.

The MCP no longer rewrites culverts: no read-back, no attribute mapping, no re-upload as
`cul_<pid>_culverts.geojson` (that name made the server think a raw EPSG:4326 table was canonical,
TASK-4042), no default-row POST. Hydrata's canonical conversion, judged by table shape, does the work.
"""

import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from tests.helpers import (
    BASE,
    EXEC_ID,
    EXEC_STATUS_URL,
    ORIGIN,
    _call_as_caller,
    _dataset,
    _exec,
    _row,
    _tool_json,
    reload_server,
)

pytestmark = pytest.mark.asyncio

SOURCE = (Path(__file__).resolve().parent.parent / "hydrata_mcp").glob("*.py")
ALL_SOURCE = "\n".join(p.read_text() for p in SOURCE)
ROUTES = {
    "boundary": "boundaries",
    "friction": "frictions",
    "inflow": "inflows",
    "rainfall": "rainfalls",
    "building": "buildings",
    "mesh_region": "mesh-regions",
    "culvert": "culverts",
}


@pytest.fixture
def _server(env_vars):
    return reload_server()


async def test_attach_one_path_the_culvert_rewrite_helpers_are_gone():
    for name in (
        "_map_culvert_collection",
        "_create_default_culvert_row",
        "_map_culvert_properties",
        "_read_culvert_features",
        "post_file_to_origin",
    ):
        assert name not in ALL_SOURCE, f"{name} is still in hydrata_mcp"


async def test_attach_one_path_no_code_names_a_cul_culverts_upload():
    assert not re.search(r"cul_\{?[a-z_]*\}?_culverts", ALL_SOURCE), "a tool still builds a cul_*_culverts file name"


@pytest.mark.parametrize("kind", list(ROUTES))
@respx.mock
async def test_attach_one_path_no_tool_uploads_a_file_for_any_kind(_server, kind):
    """Every kind: status read, list, ONE PATCH of the uploaded dataset's pk. Never a POST or a multipart."""
    route = ROUTES[kind]
    respx.get(EXEC_STATUS_URL).mock(return_value=httpx.Response(200, json=_exec("finished", [{"id": 1502}])))
    respx.get(f"{BASE}/projects/42/{route}/").mock(
        return_value=httpx.Response(200, json=[_row(9, f"{_server.INPUT_LAYER_DEFAULT_TITLES[kind]}", gn_layer=1450)])
    )
    patch = respx.patch(f"{BASE}/projects/42/{route}/9/").mock(
        return_value=httpx.Response(200, json=_row(9, "x", gn_layer=1502))
    )
    respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "base_map": 1}))
    respx.get(f"{ORIGIN}/api/v2/datasets/1502/").mock(
        return_value=httpx.Response(200, json=_dataset(1502, f"geonode:{kind}_x"))
    )
    respx.get(url__startswith=f"{ORIGIN}/api/v2/").mock(return_value=httpx.Response(200, json={"datasets": []}))
    posts = respx.post(url__regex=r".*").mock(return_value=httpx.Response(500))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "attach_input_layer",
            {"project_id": 42, "kind": kind, "execution_id": EXEC_ID, "poll_interval_seconds": 0},
        )
    )
    assert data["outcome"] == "attached"
    assert json.loads(patch.calls[0].request.content) == {"gn_layer": 1502}
    assert not posts.called, [str(c.request.url) for c in posts.calls]
