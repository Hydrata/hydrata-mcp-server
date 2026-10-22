"""The public tool list is a contract: names, descriptions, input schemas, annotations.

``tests/snapshots/tools.json`` is the committed copy. Any change to a tool (a reworded
description, a new parameter) fails here with a readable diff, so it is reviewed on purpose.
Regenerate with ``UPDATE_SNAPSHOT=1 pytest tests/test_tool_snapshot.py`` and commit the result.
A missing snapshot is never created under CI (the ``CI`` env var), so a deleted file cannot
make the gate vacuous.
"""

import difflib
import json
import os
from pathlib import Path

import pytest

from tests.test_server import _server  # noqa: F401  (shared fixture)

pytestmark = pytest.mark.asyncio

SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "tools.json"
FIELDS = ("name", "title", "description", "inputSchema", "annotations")


async def _live_tools(srv):
    tools = await srv.mcp.list_tools()
    out = []
    for tool in tools:
        dumped = tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        out.append({k: dumped[k] for k in FIELDS if k in dumped})
    return sorted(out, key=lambda t: t["name"])


def _render(tools):
    return json.dumps(tools, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


async def test_tool_list_matches_snapshot(_server):  # noqa: F811
    live = _render(await _live_tools(_server))
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(live)
        return
    if not SNAPSHOT.exists():
        if os.environ.get("CI"):
            pytest.fail(f"{SNAPSHOT} is missing; CI never creates it. Run UPDATE_SNAPSHOT=1 pytest and commit.")
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(live)
        pytest.fail(f"{SNAPSHOT} did not exist; wrote it. Review and commit it.")
    committed = SNAPSHOT.read_text()
    if committed != live:
        diff = "".join(
            difflib.unified_diff(
                committed.splitlines(keepends=True),
                live.splitlines(keepends=True),
                "tests/snapshots/tools.json (committed)",
                "live tools/list",
                n=3,
            )
        )
        pytest.fail("tool list changed; if intended run UPDATE_SNAPSHOT=1 pytest tests/test_tool_snapshot.py\n" + diff)


async def test_snapshot_covers_every_annotated_tool(_server):  # noqa: F811
    names = {t["name"] for t in json.loads(SNAPSHOT.read_text())}
    assert names == set(_server.TOOL_ANNOTATIONS)
