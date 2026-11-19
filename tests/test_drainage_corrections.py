"""TASK-4467 (epic 4445 W5.4, D16) - an agent proposes a drainage correction; a person accepts it on the map.

propose_drainage_correction posts a drainage barrier, a drainage opening or excluded ground to the terrain's
drainage-corrections API (hydrata gn_anuga/api_delineation.py, W5.1). For an agent key the API stores it as a PROPOSAL
(status 'proposed'), not applied: the tool says it is waiting and gives the link for the user. The API is mocked at its
HTTP edge. The stored proof runs this whole file.
"""

import json

import httpx
import pytest
import respx

from tests.helpers import BASE, _call_as_caller, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

CORRECTIONS = f"{BASE}/projects/42/terrain/37/drainage-corrections/"
LINE = [[153.4360, -28.5771], [153.4400, -28.5800]]
REASON = "The line's warning at CH 2+892 says the analysis cut 24.3 m through ground near the boundary; the river goes this way."
MAP_URL = "/catalogue/#/map/77?panel=catchments&line=9"


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _mock_project(base_map=77):
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": base_map}))


def _proposal(pk=21, kind="drainage_opening", status="proposed", **extra):
    return {
        "id": pk,
        "kind": kind,
        "label": "Drainage opening",
        "geometry": {"type": "LineString", "coordinates": LINE},
        "source": "drawn",
        "note": REASON,
        "status": status,
        "proposed_by_agent": True,
        "created_by": "agent-user",
        "created_at": "2026-11-17T10:00:00+00:00",
        **extra,
    }


def _reply(proposal):
    return {"terrain": 37, "corrections": [proposal], "lines_to_run_again": 0, "correction": proposal}


async def _propose(srv, **arguments):
    args = {
        "project_id": 42,
        "terrain_id": 37,
        "kind": "drainage_opening",
        "reason": REASON,
        "line": LINE,
        "delineation_id": 9,
        **arguments,
    }
    if args["delineation_id"] is None:
        del args["delineation_id"]
    return await _call_as_caller(srv, "propose_drainage_correction", args)


@respx.mock
async def test_an_agents_correction_is_stored_as_a_proposal_and_it_is_told_it_waits_with_a_link(_server):
    _mock_project()
    post = respx.post(CORRECTIONS).mock(return_value=httpx.Response(201, json=_reply(_proposal())))
    data = _tool_json(await _propose(_server))
    assert json.loads(post.calls[0].request.content) == {
        "kind": "drainage_opening",
        "note": REASON,
        "geometry": {"type": "LineString", "coordinates": LINE},
        "delineation_id": 9,  # the line asked for: the API opens it when it is on this terrain (TASK-4535)
    }
    assert data["outcome"] == "proposed" and data["status"] == "waiting"
    assert data["proposal"]["id"] == 21 and data["proposals_waiting"] == 1
    # an API that names no line to open (before TASK-4535): the line asked for, with the proposal
    assert data["ui_url"].endswith(MAP_URL + "&proposal=21") and data["line_to_open"] == {"id": 9}
    say = data["say_to_user"]
    assert say.startswith("I proposed a drainage opening on terrain 37 (proposal 21, delineation 9): ")
    assert say.endswith("Open the link and Accept or Reject it under Notices, Agent. Nothing changes until you accept.")
    assert "person accepts it on the map" in data["rule"]


@respx.mock
async def test_a_barrier_and_excluded_ground_are_proposed_in_their_own_shapes(_server):
    _mock_project()
    post = respx.post(CORRECTIONS).mock(
        return_value=httpx.Response(201, json=_reply(_proposal(kind="drainage_barrier")))
    )
    _tool_json(await _propose(_server, kind="drainage_barrier"))
    assert json.loads(post.calls[0].request.content)["geometry"]["type"] == "LineString"
    ring = [[153.43, -28.57], [153.44, -28.57], [153.44, -28.58]]
    post.mock(return_value=httpx.Response(201, json=_reply(_proposal(kind="excluded_ground"))))
    _tool_json(await _propose(_server, kind="excluded_ground", line=None, area=ring))
    sent = json.loads(post.calls[1].request.content)
    assert sent["geometry"] == {"type": "Polygon", "coordinates": [ring + [ring[0]]]}  # closed for the agent
    _tool_json(await _propose(_server, kind="excluded_ground", line=None, from_import_check=True))
    assert json.loads(post.calls[2].request.content) == {
        "kind": "excluded_ground",
        "note": REASON,
        "from_import_check": True,
        "delineation_id": 9,
    }


@respx.mock
async def test_wrong_shapes_are_refused_before_anything_is_sent(_server):
    post = respx.post(CORRECTIONS).mock(return_value=httpx.Response(201, json=_reply(_proposal())))
    for arguments in (
        {"line": [[153.43, -28.57]]},
        {"line": None},
        {"area": [[153.43, -28.57], [153.44, -28.57], [153.44, -28.58]]},
        {"kind": "excluded_ground", "line": None},
        {"kind": "excluded_ground", "line": None, "from_import_check": True, "area": [[1, 1], [2, 2], [3, 1]]},
        {"reason": " "},
        {"line": [[-28.57, 153.43], [-28.58, 153.44]]},
    ):
        assert "Nothing was sent" in _tool_error_text(await _propose(_server, **arguments))
    assert not post.called


@respx.mock
async def test_a_refusal_is_relayed_in_the_servers_words_and_nothing_changed(_server):
    _mock_project()
    respx.post(CORRECTIONS).mock(
        return_value=httpx.Response(
            403, json={"error_code": "FORBIDDEN", "detail": "You need the Editor role on this project."}
        )
    )
    data = _tool_json(await _propose(_server))
    assert data["outcome"] == "refused" and data["http_status"] == 403
    assert "You need the Editor role" in data["say_to_user"] and "Nothing changed." in data["say_to_user"]


@respx.mock
async def test_get_delineation_relays_that_the_lines_drainage_analysis_changed(_server):
    from tests.test_delineation import ONE, _doc

    _mock_project()
    changed = {"sentence": "Drainage corrections changed since this run.", "fix_sentence": "Run the line again."}
    respx.get(ONE).mock(return_value=httpx.Response(200, json=_doc(drainage_changed=changed)))
    data = _tool_json(await _call_as_caller(_server, "get_delineation", {"project_id": 42, "delineation_id": 9}))
    assert data["drainage_changed"] == changed
    assert "Drainage corrections changed since this run. Run the line again." in data["say_to_user"]


# TASK-4535 (finding F4): the link always opens a line on the proposal's terrain, with the proposal, and names the line
def _reply_naming(line, **extra):
    return {**_reply(_proposal()), "waiting": True, "line_to_open": line, "terrain_title": "Wilsons River 5 m", **extra}


@respx.mock
async def test_a_proposal_without_a_line_opens_the_line_the_server_picked_and_names_it(_server):
    _mock_project()
    post = respx.post(CORRECTIONS).mock(
        return_value=httpx.Response(201, json=_reply_naming({"id": 14, "name": "Rail"}))
    )
    data = _tool_json(await _propose(_server, delineation_id=None))
    assert "delineation_id" not in json.loads(post.calls[0].request.content)
    assert data["ui_url"].endswith("/catalogue/#/map/77?panel=catchments&line=14&proposal=21")
    assert data["line_to_open"] == {"id": 14, "name": "Rail"}
    say = data["say_to_user"]
    assert say.startswith("I proposed a drainage opening on terrain 37 (proposal 21, line Rail): ")
    assert say.endswith("Open the link and Accept or Reject it under Notices, Agent. Nothing changes until you accept.")


@respx.mock
async def test_a_proposal_with_a_line_sends_it_and_opens_the_line_the_server_answers_with(_server):
    _mock_project()
    # the line asked for is on another terrain, where the proposal would not show: the server answers with one that is
    post = respx.post(CORRECTIONS).mock(
        return_value=httpx.Response(201, json=_reply_naming({"id": 14, "name": "Rail"}))
    )
    data = _tool_json(await _propose(_server, delineation_id=9))
    assert json.loads(post.calls[0].request.content)["delineation_id"] == 9
    assert data["ui_url"].endswith("?panel=catchments&line=14&proposal=21")
    assert "line Rail" in data["say_to_user"]


@respx.mock
async def test_a_proposal_on_a_terrain_with_no_line_says_to_open_catchments_and_names_the_terrain(_server):
    _mock_project()
    respx.post(CORRECTIONS).mock(return_value=httpx.Response(201, json=_reply_naming(None)))
    data = _tool_json(await _propose(_server, delineation_id=None))
    assert data["ui_url"].endswith("/catalogue/#/map/77?panel=catchments&proposal=21")
    assert data["line_to_open"] is None
    say = data["say_to_user"]
    assert say.startswith("I proposed a drainage opening on terrain 37 (proposal 21): ")
    assert say.endswith(
        "This terrain has no line: open Catchments and choose the terrain Wilsons River 5 m, then Accept "
        "or Reject it under Notices, Agent. Nothing changes until you accept."
    )
