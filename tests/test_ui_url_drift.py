"""TASK-4099 — every panel and param the MCP's `_ui_url` can emit is handled by the map.

The MCP hands the person links like `<site>/catalogue/#/map/<id>?panel=scenarios&scenario=5&run=9`.
The map reads them in gmc's `handBackParamsEpic.js` (HAND_BACK_PARAMS + PANEL_ACTIONS). These
constants are the ONE shared list; they are pinned to the FE file when the gmc clone is next to
this repo (the dev box), and to this server's source always. A new panel or param on either side
fails here until both agree (like test_describe_input_kinds pins the kinds).

FE source: /opt/geonode-mapstore-client/geonode_mapstore_client/client/js/plugins/hydrata/Anuga/epics/handBackParamsEpic.js
"""

import re
from pathlib import Path

import pytest

from tests.helpers import reload_server

FE_PARAMS = ("panel", "layer", "scenario", "run", "kind")
# panel=<value> the FE opens (PANEL_ACTIONS keys in handBackParamsEpic.js)
FE_PANELS = ("inputs", "scenarios", "results", "hydrology", "input-review", "input-upload")

SRC = Path(__file__).resolve().parent.parent / "hydrata_mcp"
FE_FILE = Path(
    "/opt/geonode-mapstore-client/geonode_mapstore_client/client/js/plugins/hydrata/Anuga/epics/handBackParamsEpic.js"
)


def _emitted_panels() -> set[str]:
    """Every literal panel value in the server source, plus what `_scenario_panel` can return."""
    found: set[str] = set()
    for path in SRC.glob("*.py"):
        text = path.read_text()
        found.update(re.findall(r"""\bpanel\s*=\s*["']([a-z-]+)["']""", text))
        found.update(re.findall(r"""["']panel["']\s*:\s*["']([a-z-]+)["']""", text))
    scenarios = (SRC / "scenarios.py").read_text()
    body = scenarios[scenarios.index("def _scenario_panel") :].split("\ndef ", 1)[0]
    found.update(re.findall(r"""return\s+["']([a-z-]+)["']""", body))
    return found


@pytest.fixture
def _helpers(env_vars):
    reload_server()
    from hydrata_mcp import _helpers as helpers

    return helpers


def _emitted_params(_helpers) -> set[str]:
    found = set(_helpers.HAND_BACK_PARAM_ORDER)
    for path in SRC.glob("*.py"):
        for call in re.findall(r"_ui_url\(([^)]*)\)", path.read_text()):
            found.update(re.findall(r"\b([a-z_]+)=", call))
    return found


def test_ui_url_drift_every_panel_the_mcp_can_emit_is_handled_by_the_map():
    emitted = _emitted_panels()
    assert emitted, "found no panel values: the scan is broken"
    assert emitted <= set(FE_PANELS), f"MCP emits a panel the map ignores: {sorted(emitted - set(FE_PANELS))}"


def test_ui_url_drift_every_param_the_mcp_can_emit_is_handled_by_the_map(_helpers):
    emitted = _emitted_params(_helpers)
    assert {"panel", "scenario", "run"} <= emitted
    assert emitted <= set(FE_PARAMS), f"MCP emits a param the map ignores: {sorted(emitted - set(FE_PARAMS))}"
    assert tuple(_helpers.HAND_BACK_PARAM_ORDER) == FE_PARAMS


def test_ui_url_drift_the_constants_match_the_fe_file_when_the_gmc_clone_is_here():
    if not FE_FILE.exists():  # CI has no gmc clone: the server-side pins above still run
        return
    text = FE_FILE.read_text()
    params = re.search(r"HAND_BACK_PARAMS\s*=\s*\[([^\]]*)\]", text).group(1)
    assert tuple(re.findall(r"'([a-z]+)'", params)) == FE_PARAMS
    block = text[text.index("const PANEL_ACTIONS") : text.index("/**\n * The hash with the hand-back")]
    panels = re.findall(r"^\s{4}(?:'([a-z-]+)'|([a-z]+)):\s*\(", block, re.M)
    assert tuple(a or b for a, b in panels) == FE_PANELS


def test_ui_url_drift_run_link_shape(monkeypatch, _helpers):
    monkeypatch.setattr(_helpers, "_ui_origin", lambda: "https://x.test")
    assert _helpers._ui_url(8938, run=1608) == "https://x.test/catalogue/#/map/8938?run=1608"
