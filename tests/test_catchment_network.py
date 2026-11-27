"""Epic 4445 W11.4 - agents create, read and edit sub-catchments through the MCP; a dragged boundary is a proposal.

Four tools on the sub-catchments API (hydrata gn_anuga/api_catchment_network.py, api_sub_catchment_proposals.py):

* create_sub_catchments - POST sub-catchments/ (or <id>/create/ to create a set again); a fast-screen line answers
  400 'High res only' (D20), relayed in the new words.
* get_sub_catchments - the sets of a line, or one set read compactly (no polygons), waiting for a cloud creation.
* edit_sub_catchments - divide, merge, move a node, add a forced point, undo a dragged boundary (edits/); 'make this a
  link' is a PROPOSAL (proposals/) that changes nothing until a person accepts it (D16, D22).
* propose_sub_catchment_boundary - a dragged boundary, always filed WAITING: the agent is told it waits and gets a link.

Words (Decision 889): sub-catchments, create and divide; never 'network' or 'cut' in anything an agent reads. The API is
mocked at its HTTP edge. The stored proof runs this whole file.
"""

import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from tests.helpers import BASE, _call_as_caller, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

LIST = f"{BASE}/projects/42/sub-catchments/"
SET = f"{LIST}7/"
MAP_URL = "/catalogue/#/map/77?panel=catchments&line=9"
TOOLS = (
    "create_sub_catchments",
    "get_sub_catchments",
    "edit_sub_catchments",
    "propose_sub_catchment_boundary",
    "propose_culvert_openings",
)
FORBIDDEN = re.compile(r"\bnetworks?\b|\bcut\b|\bcuts\b|\bcutting\b|promot", re.I)
REASON = "The road embankment holds water on the east side; the boundary follows its crest."
LINE = [[149.1001, -35.2501], [149.1010, -35.2510], [149.1020, -35.2512]]
HIGH_RES_ONLY = "High res only. Run the line at High resolution, then create sub-catchments."


@pytest.fixture
def _server(env_vars):
    return reload_server()


def _mock_project(base_map=77):
    respx.get(url__regex=r".*/projects/42/?$").mock(return_value=httpx.Response(200, json={"base_map": base_map}))


def _fact(value, unit, definition):
    return {"value": value, "unit": unit, "definition": definition, "source": "the kept drainage analysis"}


def _sub(pk, label, node, area_ha, notes=()):
    return {
        "id": pk,
        "label": label,
        "drains_to": node,
        "drains_to_outside": None,
        "inflow_cell": [10, label],
        "line_catchment_id": 501,
        "area_m2": area_ha * 10_000,
        "area_ha": area_ha,
        "polygon": {
            "type": "Polygon",
            "coordinates": [[[149.1, -35.2], [149.2, -35.2], [149.2, -35.3], [149.1, -35.2]]],
        },
        "label_point": [149.15, -35.25],
        "facts": {
            "area": _fact(area_ha * 10_000, "m2", "The ground that drains to the node."),
            "mean_land_slope": _fact(0.031, "m/m", "The mean slope of the ground."),
        },
        "notes": list(notes),
    }


NOTE = {
    "id": 88,
    "kind": "can_share_water",
    "other": 12,
    "line_link_id": 640,
    "rise_m": 0.5,
    "within_accuracy": False,
    "lon": 149.11,
    "lat": -35.25,
    "boundary_bearing_deg": 90.0,
}


def _doc(**version):
    nodes = [
        {
            "id": 31,
            "uid": "u-31",
            "kind": "drainage_point",
            "name": "Outlet A",
            "lon": 149.1,
            "lat": -35.26,
            "level_m": 581.2,
            "line_catchment_id": 501,
            "source_ref": None,
            "total_catchment": {"area": _fact(120_000.0, "m2", "All the ground that drains to the node.")},
        },
        {
            "id": 32,
            "uid": "u-32",
            "kind": "confluence",
            "name": "",
            "lon": 149.11,
            "lat": -35.255,
            "level_m": 583.0,
            "line_catchment_id": 501,
            "source_ref": None,
            "total_catchment": {},
        },
    ]
    base = {
        "number": 1,
        "status": "draft",
        "based_on": None,
        "how_created": {
            "by": "target",
            "target_count": 20,
            "threshold_m2": 43_000.0,
            "min_area_m2": 1_000.0,
            "forced_points": [],
        },
        "sizes": {"count": 2, "node_count": 2, "smallest_m2": 40_000.0, "median_m2": 60_000.0, "largest_m2": 80_000.0},
        "made_from": {"line_computed_at": "2026-11-21T01:00:00+00:00"},
        "confirmed_by": None,
        "confirmed_at": None,
        "created_at": "2026-11-21T02:00:00+00:00",
        "line_changed_since": False,
        "culverts_changed_since": False,
        "culverts": {"count": 0, "flagged": 0, "openings_offered": 0, "placed": [], "not_placed": []},
        "nodes": nodes,
        "sub_catchments": [_sub(11, 1, 31, 8.0, [NOTE]), _sub(12, 2, 32, 4.0)],
        "drags": [],
        "proposals": [],
        "links": [
            {
                "id": 51,
                "kind": "reach",
                "from_node": 32,
                "to_node": 31,
                "from_outside": None,
                "to_outside": None,
                "line_link_id": None,
                "geometry": {"type": "LineString", "coordinates": [[1, 2], [3, 4]]},
                "facts": {"length": _fact(310.0, "m", "The length of the reach.")},
            }
        ],
        **version,
    }
    return {
        "id": 7,
        "project_id": 42,
        "delineation_id": 9,
        "line_name": "Kaleen toe",
        "name": "",
        "catchment_ids": [501],
        "created_by": "agent-user",
        "created_at": "2026-11-21T02:00:00+00:00",
        "versions": [{k: base[k] for k in ("number", "status", "based_on", "confirmed_by", "line_changed_since")}],
        "edits": 1,
        "version": base,
        "job": None,
    }


def _words(text):
    return FORBIDDEN.findall(text)


# ---------------------------------------------------------------------------
# create_sub_catchments
# ---------------------------------------------------------------------------
@respx.mock
async def test_create_sub_catchments_posts_the_line_and_answers_the_set_compactly_with_a_link(_server):
    _mock_project()
    post = respx.post(LIST).mock(return_value=httpx.Response(201, json=_doc()))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "create_sub_catchments",
            {
                "project_id": 42,
                "delineation_id": 9,
                "max_area_ha": 12.0,
                "catchment_ids": [501],
                "forced_points": [[149.105, -35.255]],
                "name": "Agent set",
            },
        )
    )
    assert json.loads(post.calls[0].request.content) == {
        "delineation_id": 9,
        "max_area_ha": 12.0,
        "catchment_ids": [501],
        "forced_points": [{"lon": 149.105, "lat": -35.255}],
        "name": "Agent set",
    }
    assert data["outcome"] == "created" and data["sub_catchments_id"] == 7 and data["delineation_id"] == 9
    assert data["version"]["status"] == "draft" and data["version"]["counts"] == {"sub_catchments": 2, "nodes": 2}
    assert data["ui_url"].endswith(MAP_URL)
    assert "polygon" not in json.dumps(data) and "geometry" not in json.dumps(data)  # shapes stay on the map
    say = data["say_to_user"]
    assert say.startswith("I divided Kaleen toe")
    assert "a person confirms them on the map" in say
    assert not _words(json.dumps(data)), _words(json.dumps(data))


@respx.mock
async def test_create_sub_catchments_again_posts_to_the_set_without_the_line(_server):
    _mock_project()
    post = respx.post(f"{SET}create/").mock(return_value=httpx.Response(200, json=_doc()))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "create_sub_catchments",
            {"project_id": 42, "delineation_id": 9, "sub_catchments_id": 7, "max_area_ha": 5},
        )
    )
    assert json.loads(post.calls[0].request.content) == {"max_area_ha": 5.0}
    assert data["outcome"] == "created_again"


@respx.mock
async def test_create_sub_catchments_on_a_fast_screen_relays_high_res_only(_server):
    """Re-grill 19-11-26 (D20) in the words of Decision 889: the engine's refusal for a fast-screen result."""
    _mock_project()
    respx.post(LIST).mock(
        return_value=httpx.Response(400, json={"error_code": "HIGH_RES_ONLY", "detail": HIGH_RES_ONLY})
    )
    data = _tool_json(await _call_as_caller(_server, "create_sub_catchments", {"project_id": 42, "delineation_id": 9}))
    assert data["outcome"] == "refused" and data["error_code"] == "HIGH_RES_ONLY" and data["http_status"] == 400
    assert HIGH_RES_ONLY in data["say_to_user"] and "Nothing changed." in data["say_to_user"]
    assert "delineate_catchments" in data["next_step"]  # how to run it at High resolution
    assert not _words(data["say_to_user"])


@respx.mock
async def test_create_sub_catchments_on_cloud_compute_answers_creating_and_how_to_wait(_server):
    _mock_project()
    doc = _doc()
    doc["version"] = None
    doc["job"] = {
        "process_id": "p-1",
        "runs_on": "cloud",
        "state": "running",
        "sentence": "61,000,000 cells: creating sub-catchments on cloud compute. They appear here when it is done.",
    }
    respx.post(LIST).mock(return_value=httpx.Response(202, json=doc))
    data = _tool_json(await _call_as_caller(_server, "create_sub_catchments", {"project_id": 42, "delineation_id": 9}))
    assert data["outcome"] == "creating" and data["job"]["state"] == "running"
    assert "get_sub_catchments" in data["next_step"] and "cloud compute" in data["say_to_user"]


@respx.mock
async def test_create_sub_catchments_refuses_both_a_target_and_a_threshold_before_sending(_server):
    # epic 4674 W4 (TASK-4699): the tool takes neither any more; a call with them is refused before anything is sent
    post = respx.post(LIST).mock(return_value=httpx.Response(201, json=_doc()))
    text = _tool_error_text(
        await _call_as_caller(
            _server,
            "create_sub_catchments",
            {"project_id": 42, "delineation_id": 9, "target_count": 10, "threshold_m2": 1000},
        )
    )
    assert "target_count" in text and "threshold_m2" in text and not post.called


@respx.mock
async def test_an_agent_key_refused_on_a_persons_set_is_told_why(_server):
    _mock_project()
    respx.post(f"{SET}create/").mock(
        return_value=httpx.Response(
            403,
            json={
                "error_code": "AGENT_KEY_SCOPE",
                "detail": "An agent key cannot create again the sub-catchments a "
                "person made. Create a new set, or ask the person.",
            },
        )
    )
    data = _tool_json(
        await _call_as_caller(
            _server, "create_sub_catchments", {"project_id": 42, "delineation_id": 9, "sub_catchments_id": 7}
        )
    )
    assert data["outcome"] == "refused" and data["error_code"] == "AGENT_KEY_SCOPE"
    assert "a person made" in data["say_to_user"]


# ---------------------------------------------------------------------------
# get_sub_catchments
# ---------------------------------------------------------------------------
@respx.mock
async def test_get_sub_catchments_lists_the_sets_of_a_line(_server):
    route = respx.get(LIST).mock(return_value=httpx.Response(200, json={"count": 1, "results": [_doc()]}))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "delineation_id": 9}))
    assert route.calls[0].request.url.params["delineation_id"] == "9"
    assert data["count"] == 1
    one = data["results"][0]
    assert one["sub_catchments_id"] == 7 and one["version"]["status"] == "draft"
    assert "sub_catchments" not in one["version"]  # the list is a summary; read one set for its pieces


@respx.mock
async def test_get_sub_catchments_names_the_set_the_person_sees_and_adopting_carries(_server):
    """TASK-4600 (Gate 3 pre-walk F1): a newer agent draft never takes over the person's confirmed set; the list says which
    set step 3 shows and adopt_catchments carries, and that the newer one waits beside it."""
    newer = {
        "id": 8,
        "made_by_agent": True,
        "sentence": "An agent made a draft set on 23-11-26.",
        "showing_sentence": "This is the draft set an agent made on 23-11-26.",
    }
    body = {"count": 2, "results": [_doc(), {**_doc(), "id": 8}], "shown_id": 7, "newer": newer}
    respx.get(LIST).mock(return_value=httpx.Response(200, json=body))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "delineation_id": 9}))
    assert data["line_set"]["sub_catchments_id"] == 7
    assert data["line_set"]["newer_sub_catchments_id"] == 8
    words = data["line_set"]["rule"]
    assert "step 3" in words and "adopt_catchments" in words
    assert "An agent made a draft set on 23-11-26." in data["say_to_user"]
    assert "confirm them again" not in words.lower()


@respx.mock
async def test_get_sub_catchments_lists_a_line_with_no_named_set_as_before(_server):
    respx.get(LIST).mock(return_value=httpx.Response(200, json={"count": 1, "results": [_doc()]}))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "delineation_id": 9}))
    assert "line_set" not in data and "say_to_user" not in data


@respx.mock
async def test_get_sub_catchments_reads_one_set_with_its_facts_and_no_shapes(_server):
    doc = _doc(
        line_changed_since=True,
        line_changed_sentence="The line has run again since these were created.",
        fix_sentence="Create again from the new result.",
        create_again_drops=1,
        create_again_sentence="Creating again drops 1 edit made by hand.",
        drags=[{"drag_id": "ab12", "seq": 3, "state": "waiting", "state_label": "Waiting", "barrier_id": None}],
        proposals=[
            {
                "id": 5,
                "kind": "make_link",
                "label": "Make this a link",
                "status": "proposed",
                "note": REASON,
                "inputs": {"line_link_id": 640},
                "made_by": "agent-user",
                "made_by_agent": True,
                "note_id": 88,
                "created_at": "2026-11-21T03:00:00+00:00",
                "decided_by": None,
                "decided_at": None,
            }
        ],
    )
    respx.get(SET).mock(return_value=httpx.Response(200, json=doc))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    version = data["version"]
    assert data["outcome"] == "ready"
    one = version["sub_catchments"][0]
    assert one == {
        "id": 11,
        "label": 1,
        "drains_to": 31,
        "drains_to_outside": None,
        "line_catchment_id": 501,
        "area_ha": 8.0,
        "label_point": [149.15, -35.25],
        "facts": {"area": 80_000.0, "mean_land_slope": 0.031},
        "notes": [
            {
                "id": 88,
                "kind": "can_share_water",
                "other": 12,
                "rise_m": 0.5,
                "within_accuracy": False,
                "lon": 149.11,
                "lat": -35.25,
            }
        ],
    }
    assert data["fact_definitions"]["mean_land_slope"] == {
        "unit": "m/m",
        "definition": "The mean slope of the ground.",
        "source": "the kept drainage analysis",
    }
    assert data["fact_definitions"]["length"]["unit"] == "m"
    assert version["nodes"][0]["total_catchment"] == {"area": 120_000.0} and version["nodes"][0]["name"] == "Outlet A"
    assert version["links"] == [
        {
            "id": 51,
            "kind": "reach",
            "from_node": 32,
            "to_node": 31,
            "from_outside": None,
            "to_outside": None,
            "line_link_id": None,
            "facts": {"length": 310.0},
        }
    ]
    assert "polygon" not in json.dumps(data) and "coordinates" not in json.dumps(data)
    assert version["drags"][0]["state"] == "waiting" and version["proposals"][0]["note_id"] == 88
    say = data["say_to_user"]
    assert "The line has run again since these were created. Create again from the new result." in say
    assert "1 dragged boundary is waiting for a person" in say and "1 proposal is waiting for a person" in say
    assert not _words(json.dumps(data)), _words(json.dumps(data))


@respx.mock
async def test_get_sub_catchments_relays_a_set_created_by_an_earlier_rule(_server):
    # W12.5 review (TASK-4576, D16): the screen's blocker row says the set was created by an earlier rule; the agent hears
    # the same sentence and its fix, and what creating again drops
    doc = _doc(
        line_changed_since=False,
        earlier_rule=True,
        earlier_rule_sentence="These sub-catchments were created by an earlier rule. Ground reaching a drainage point "
        "directly is now one sub-catchment per side of the point along the line.",
        fix_sentence="Create again.",
        create_again_drops=2,
        create_again_sentence="Creating again drops 2 edits made by hand.",
    )
    respx.get(SET).mock(return_value=httpx.Response(200, json=doc))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    say = data["say_to_user"]
    assert "These sub-catchments were created by an earlier rule." in say and "Create again." in say
    assert "Creating again drops 2 edits made by hand." in say
    assert not _words(json.dumps(data)), _words(json.dumps(data))


@respx.mock
async def test_get_sub_catchments_gives_the_outlines_asked_for_and_no_others(_server):
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    data = _tool_json(
        await _call_as_caller(
            _server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "outlines_of": [12]}
        )
    )
    by_id = {s["id"]: s for s in data["version"]["sub_catchments"]}
    assert "outline" not in by_id[11] and by_id[12]["outline"]["type"] == "Polygon"
    assert by_id[12]["outline"]["coordinates"][0][0] == [149.1, -35.2]
    text = _tool_error_text(
        await _call_as_caller(
            _server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "outlines_of": list(range(11))}
        )
    )
    assert "at most 10" in text


STREAMS = {
    "sub_catchments_id": 7,
    "version": 2,
    "threshold_m2": 39000.0,
    "threshold_ha": 3.9,
    "definition": "Streams: every place at least 3.9 ha drains through, at the threshold these sub-catchments were "
    "created with. A divide goes on the stream nearest the click.",
    "no_stream_sentence": "No stream here: add a forced point instead.",
    "sub_catchments": [
        {
            "id": 11,
            "label": 1,
            "has_stream": True,
            "stream_cells": 3,
            "streams": {"type": "MultiLineString", "coordinates": [[[149.12345678, -35.2], [149.1236, -35.2001]]]},
        },
        {
            "id": 12,
            "label": 2,
            "has_stream": False,
            "stream_cells": 0,
            "streams": {"type": "MultiLineString", "coordinates": []},
        },
    ],
}


@respx.mock
async def test_get_sub_catchments_gives_the_streams_a_divide_goes_on_and_marks_a_sub_catchment_with_none(_server):
    """TASK-4575 (W12.9, D16): the streams step 3 draws (where a divide goes) are read the same way by an agent."""
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    streams = respx.get(f"{SET}streams/").mock(return_value=httpx.Response(200, json=STREAMS))
    data = _tool_json(
        await _call_as_caller(
            _server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "streams_of": [11]}
        )
    )
    assert streams.called
    by_id = {s["id"]: s for s in data["version"]["sub_catchments"]}
    assert by_id[11]["has_stream"] is True and by_id[12]["has_stream"] is False
    assert by_id[11]["streams"] == {
        "type": "MultiLineString",
        "coordinates": [[[149.123457, -35.2], [149.1236, -35.2001]]],
    }
    assert "streams" not in by_id[12]
    assert data["streams"]["no_stream_sentence"] == "No stream here: add a forced point instead."
    assert data["streams"]["threshold_m2"] == 39000.0 and "divide" in data["streams"]["definition"]
    assert not _words(json.dumps(data)), _words(json.dumps(data))


@respx.mock
async def test_get_sub_catchments_reads_no_streams_unless_asked_and_relays_a_refusal(_server):
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    streams = respx.get(f"{SET}streams/").mock(
        return_value=httpx.Response(
            409,
            json={
                "error_code": "TOO_LARGE",
                "detail": "These sub-catchments are too large to change on the web server. Nothing was changed.",
            },
        )
    )
    plain = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    assert not streams.called and "streams" not in plain
    assert all("has_stream" not in s for s in plain["version"]["sub_catchments"])
    refused = _tool_json(
        await _call_as_caller(
            _server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "streams_of": [11]}
        )
    )
    assert "too large" in refused["streams"]["sentence"]
    text = _tool_error_text(
        await _call_as_caller(
            _server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "streams_of": list(range(11))}
        )
    )
    assert "at most 10" in text


@respx.mock
async def test_get_sub_catchments_waits_for_a_creation_on_cloud_compute(_server):
    running = _doc()
    running["version"] = None
    running["job"] = {"process_id": "p-1", "runs_on": "cloud", "state": "running", "sentence": "creating"}
    respx.get(SET).mock(side_effect=[httpx.Response(200, json=running), httpx.Response(200, json=_doc())])
    data = _tool_json(
        await _call_as_caller(
            _server,
            "get_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "timeout_seconds": 5, "poll_interval_seconds": 0},
        )
    )
    assert data["outcome"] == "ready" and data["polls"] == 2 and data["version"]["counts"]["sub_catchments"] == 2


# ---------------------------------------------------------------------------
# edit_sub_catchments
# ---------------------------------------------------------------------------
def _edited(kind, **inputs):
    doc = _doc()
    doc["edit"] = {
        "seq": 2,
        "kind": kind,
        "label": "Divide a sub-catchment",
        "inputs": inputs,
        "made_by": "agent-user",
        "made_by_agent": True,
        "made_at": "2026-11-21T03:00:00+00:00",
        "version": 1,
    }
    doc["remeasured"] = [1, 3]
    return doc


@respx.mock
async def test_edit_sub_catchments_divides_one_on_the_draft(_server):
    _mock_project()
    post = respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_edited("divide")))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "edit_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "kind": "divide", "lon": 149.105, "lat": -35.255},
        )
    )
    assert json.loads(post.calls[0].request.content) == {"kind": "divide", "lon": 149.105, "lat": -35.255}
    assert data["outcome"] == "changed" and data["edit"]["label"] == "Divide a sub-catchment"
    assert data["remeasured"] == [1, 3] and data["ui_url"].endswith(MAP_URL)
    assert data["say_to_user"].startswith(
        "Divide a sub-catchment: done on the draft of the sub-catchments of Kaleen toe"
    )


@respx.mock
async def test_edit_sub_catchments_sends_each_edit_in_its_own_shape(_server):
    _mock_project()
    post = respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_edited("merge")))
    calls = [
        ({"kind": "merge", "sub_catchment_ids": [11, 12]}, {"kind": "merge", "sub_catchment_ids": [11, 12]}),
        (
            {"kind": "move_node", "node_id": 32, "lon": 149.1, "lat": -35.2},
            {"kind": "move_node", "node_id": 32, "lon": 149.1, "lat": -35.2},
        ),
        (
            {"kind": "add_forced_point", "lon": 149.1, "lat": -35.2},
            {"kind": "add_forced_point", "lon": 149.1, "lat": -35.2, "node_kind": "forced_point"},
        ),
        ({"kind": "undo_drag", "drag_id": "ab12"}, {"kind": "undo_drag", "drag_id": "ab12"}),
    ]
    for i, (arguments, sent) in enumerate(calls):
        _tool_json(
            await _call_as_caller(
                _server, "edit_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, **arguments}
            )
        )
        assert json.loads(post.calls[i].request.content) == sent


@respx.mock
async def test_edit_sub_catchments_relays_what_a_merge_did_naming_every_sub_catchment_joined(_server):
    # TASK-4607 (pre-walk P1): a merge across a diverted point joins three; the answer says so, and the agent relays it
    _mock_project()
    doc = _edited("merge")
    doc["edit"]["label"] = "Merge two sub-catchments"
    doc["edit"]["sentence"] = "The diverted point goes: sub-catchments 15, 16 and 19 become one, now sub-catchment 15."
    respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=doc))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "edit_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "kind": "merge", "sub_catchment_ids": [15, 19]},
        )
    )
    assert data["edit"]["sentence"] == doc["edit"]["sentence"]
    assert (
        "The diverted point goes: sub-catchments 15, 16 and 19 become one, now sub-catchment 15." in data["say_to_user"]
    )
    # an edit the server says nothing more about has no sentence
    respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_edited("divide")))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "edit_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "kind": "divide", "lon": 149.1, "lat": -35.2},
        )
    )
    assert "sentence" not in data["edit"]


@respx.mock
async def test_edit_sub_catchments_refuses_a_wrong_shape_before_sending(_server):
    post = respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_edited("divide")))
    for arguments in (
        {"kind": "divide"},
        {"kind": "merge", "sub_catchment_ids": [11]},
        {"kind": "move_node", "lon": 149.1, "lat": -35.2},
        {"kind": "make_link", "note_id": 88},  # a proposal says why
        {"kind": "undo_drag"},
    ):
        _tool_error_text(
            await _call_as_caller(
                _server, "edit_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, **arguments}
            )
        )
    assert not post.called


@respx.mock
async def test_edit_sub_catchments_undoes_one_edit_made_by_hand_by_its_seq(_server):
    # review of W11 (TASK-4484 added 'undo' for the map's Undo last and History; D16: tools and screen at parity)
    _mock_project()
    post = respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_edited("undo", seq=3)))
    data = _tool_json(
        await _call_as_caller(
            _server, "edit_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "kind": "undo", "seq": 3}
        )
    )
    assert json.loads(post.calls[0].request.content) == {"kind": "undo", "seq": 3}
    assert data["outcome"] == "changed"
    _tool_error_text(
        await _call_as_caller(
            _server, "edit_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "kind": "undo"}
        )
    )
    assert post.call_count == 1


@respx.mock
async def test_get_sub_catchments_lists_the_edits_made_by_hand_an_undo_names(_server):
    _mock_project()
    hand = [
        {"seq": 2, "kind": "add_forced_point", "label": "Add a forced point", "made_by_agent": False},
        {
            "seq": 3,
            "kind": "drag",
            "label": "Drag a boundary",
            "made_by_agent": True,
            "drag_id": "ab12",
            "state": "waiting",
            "line": [[1, 2], [3, 4]],
            "moved_ha": 0.4,
        },
    ]
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc(hand_edits=hand)))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    assert data["version"]["hand_edits"] == [
        {"seq": 2, "kind": "add_forced_point", "label": "Add a forced point", "made_by_agent": False},
        {
            "seq": 3,
            "kind": "drag",
            "label": "Drag a boundary",
            "made_by_agent": True,
            "drag_id": "ab12",
            "state": "waiting",
        },
    ]


@respx.mock
async def test_an_agents_make_this_a_link_is_a_proposal_that_changes_nothing(_server):
    """Re-grill 19-11-26 (D22 with D16): the tool files a proposal and changes nothing; a person accepts it on the map."""
    _mock_project()
    edits = respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_edited("make_link")))
    proposal = {
        "id": 5,
        "kind": "make_link",
        "label": "Make this a link",
        "status": "proposed",
        "note": REASON,
        "inputs": {"line_link_id": 640, "rise_m": 0.5},
        "made_by": "agent-user",
        "made_by_agent": True,
        "created_at": "2026-11-21T03:00:00+00:00",
        "decided_by": None,
        "decided_at": None,
        "sentence": "Proposed: make this a link. A person accepts or rejects it on the map.",
    }
    post = respx.post(f"{SET}proposals/").mock(return_value=httpx.Response(201, json=proposal))
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "edit_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "kind": "make_link", "note_id": 88, "reason": REASON},
        )
    )
    assert json.loads(post.calls[0].request.content) == {"kind": "make_link", "note_id": 88, "note": REASON}
    assert not edits.called  # nothing is changed by the tool itself
    assert data["outcome"] == "proposed" and data["status"] == "waiting" and data["proposal"]["id"] == 5
    assert data["ui_url"].endswith(MAP_URL)
    say = data["say_to_user"]
    assert say.startswith("I proposed making a 'can share water' note a link (proposal 5")
    assert "Nothing changes until you accept it" in say and "on the map" in say
    assert "person accepts" in data["rule"]


@respx.mock
async def test_a_make_link_proposal_says_its_rise_to_two_decimals(_server):
    """TASK-4608 (Gate 3 pre-walk P5): the sentence to the person says 'a rise of 0.37 m', not 0.374146; the data keeps it."""
    _mock_project()
    proposal = {
        "id": 6,
        "kind": "make_link",
        "label": "Make this a link",
        "status": "proposed",
        "note": REASON,
        "inputs": {"line_link_id": 640, "rise_m": 0.374146},
        "made_by": "agent-user",
        "made_by_agent": True,
        "created_at": "2026-11-23T03:00:00+00:00",
        "decided_by": None,
        "decided_at": None,
        "sentence": "Proposed: make this a link. A person accepts or rejects it on the map.",
    }
    respx.post(f"{SET}proposals/").mock(return_value=httpx.Response(201, json=proposal))
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "edit_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "kind": "make_link", "note_id": 88, "reason": REASON},
        )
    )
    assert "(proposal 6, a rise of 0.37 m)" in data["say_to_user"]
    assert data["proposal"]["rise_m"] == 0.374146


@respx.mock
async def test_a_make_link_proposal_names_its_note_and_both_sub_catchments(_server):
    """TASK-4604 (Gate 3 pre-walk F5): the answer carries the note id and the two sub-catchments, and says them."""
    _mock_project()
    proposal = {
        "id": 7,
        "kind": "make_link",
        "label": "Make this a link",
        "status": "proposed",
        "note": REASON,
        "inputs": {"line_link_id": 640, "rise_m": 0.5},
        "note_id": 88,
        "sub_catchment_labels": [9, 6],
        "made_by": "agent-user",
        "made_by_agent": True,
        "created_at": "2026-11-23T03:00:00+00:00",
        "decided_by": None,
        "decided_at": None,
        "sentence": "Proposed: make this a link. A person accepts or rejects it on the map.",
    }
    respx.post(f"{SET}proposals/").mock(return_value=httpx.Response(201, json=proposal))
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "edit_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "kind": "make_link", "note_id": 88, "reason": REASON},
        )
    )
    assert data["proposal"]["note_id"] == 88 and data["proposal"]["sub_catchment_labels"] == [9, 6]
    assert "between sub-catchments 6 and 9" in data["say_to_user"]


@respx.mock
async def test_a_make_link_that_would_loop_is_refused_when_filed_and_names_its_note(_server):
    """TASK-4604: the server checks the loop when the agent files the link; the refusal is relayed with its note id."""
    _mock_project()
    sentence = (
        "Refused: this link makes a loop. The water of sub-catchment 7 already reaches sub-catchment 6, so it would "
        "come back to where it started."
    )
    respx.post(f"{SET}proposals/").mock(
        return_value=httpx.Response(409, json={"error_code": "LOOP", "detail": sentence, "note_id": 88})
    )
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "edit_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "kind": "make_link", "note_id": 88, "reason": REASON},
        )
    )
    assert data["outcome"] == "refused" and data["error_code"] == "LOOP" and data["note_id"] == 88
    assert "makes a loop" in data["say_to_user"] and "Nothing changed." in data["say_to_user"]
    assert "proposal" not in data


# ---------------------------------------------------------------------------
# propose_sub_catchment_boundary
# ---------------------------------------------------------------------------
def _dragged(state="waiting"):
    doc = _edited("drag", drag_id="ab12", state=state, moved_m2=2_500.0)
    doc["edit"]["label"] = "Drag a boundary"
    doc["version"]["drags"] = [
        {"drag_id": "ab12", "seq": 2, "state": state, "state_label": "Waiting", "barrier_id": None}
    ]
    return doc


@respx.mock
async def test_a_dragged_boundary_from_an_agent_is_filed_waiting_and_the_agent_gets_a_link(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))  # the labels, read before the edit
    post = respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_dragged()))
    terrain = respx.post(f"{SET}drags/ab12/terrain/")
    data = _tool_json(
        await _call_as_caller(
            _server,
            "propose_sub_catchment_boundary",
            {"project_id": 42, "sub_catchments_id": 7, "sub_catchment_ids": [11, 12], "line": LINE, "reason": REASON},
        )
    )
    # TASK-4606 (Gate 3 pre-walk F8): the reason goes with the drag as its note, which the person reads on the Decide card
    assert json.loads(post.calls[0].request.content) == {
        "kind": "drag",
        "sub_catchment_ids": [11, 12],
        "line": {"type": "LineString", "coordinates": LINE},
        "state": "waiting",
        "note": REASON,
    }
    assert not terrain.called
    assert data["outcome"] == "proposed" and data["status"] == "waiting" and data["drag_id"] == "ab12"
    assert data["moved_ha"] == 0.25 and data["ui_url"].endswith(MAP_URL)
    say = data["say_to_user"]
    assert say.startswith(
        "I proposed a new boundary between sub-catchments 1 and 2 of Kaleen toe (dragged boundary ab12)"
    )
    assert REASON in say
    assert "It is waiting" in say and "Keep as drawn or Make the terrain agree" in say
    assert "cannot be confirmed while it waits" in say
    assert "only a person" in data["rule"].lower()


@respx.mock
async def test_a_proposed_boundary_can_carry_the_terrain_preview(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_dragged()))
    preview = {
        "drag_id": "ab12",
        "preview": True,
        "stored": False,
        "barrier": {"type": "LineString", "coordinates": []},
        "difference": {
            "agree_pct": 92.0,
            "differ_ha": 0.02,
            "max_distance_m": 4.0,
            "larger_than_dragged": False,
            "polygons": {"type": "MultiPolygon", "coordinates": []},
        },
        "sentence": "With the barrier, 92% of the dragged ground drains as drawn; 0.02 ha drains elsewhere, up to 4 m "
        "from the line.",
    }
    terrain = respx.post(f"{SET}drags/ab12/terrain/").mock(return_value=httpx.Response(200, json=preview))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "propose_sub_catchment_boundary",
            {
                "project_id": 42,
                "sub_catchments_id": 7,
                "sub_catchment_ids": [11, 12],
                "line": LINE,
                "reason": REASON,
                "preview_terrain": True,
            },
        )
    )
    assert json.loads(terrain.calls[0].request.content) == {"preview": True}
    assert data["terrain_preview"]["sentence"].startswith("With the barrier, 92% of the dragged ground drains as drawn")
    assert "coordinates" not in json.dumps(data["terrain_preview"])
    assert "92% of the dragged ground drains as drawn" in data["say_to_user"]


@respx.mock
async def test_a_refused_drag_is_relayed_and_nothing_changed(_server):
    _mock_project()
    respx.post(f"{SET}edits/").mock(
        return_value=httpx.Response(
            409,
            json={
                "error_code": "EDIT_REFUSED",
                "detail": "Refused: start and end the line on the boundary between the two sub-catchments.",
            },
        )
    )
    data = _tool_json(
        await _call_as_caller(
            _server,
            "propose_sub_catchment_boundary",
            {"project_id": 42, "sub_catchments_id": 7, "sub_catchment_ids": [11, 12], "line": LINE, "reason": REASON},
        )
    )
    assert data["outcome"] == "refused" and data["error_code"] == "EDIT_REFUSED"
    assert "start and end the line on the boundary" in data["say_to_user"] and "Nothing changed." in data["say_to_user"]


@respx.mock
async def test_a_proposed_boundary_needs_two_sub_catchments_a_line_and_a_reason(_server):
    post = respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_dragged()))
    good = {"project_id": 42, "sub_catchments_id": 7, "sub_catchment_ids": [11, 12], "line": LINE, "reason": REASON}
    for change in (
        {"sub_catchment_ids": [11]},
        {"sub_catchment_ids": [11, 11]},
        {"line": LINE[:1]},
        {"reason": " "},
        {"reason": "x" * 201},
    ):
        _tool_error_text(await _call_as_caller(_server, "propose_sub_catchment_boundary", {**good, **change}))
    assert not post.called


# ---------------------------------------------------------------------------
# The words (Decision 889) and the tool list
# ---------------------------------------------------------------------------
async def test_the_four_tools_never_say_network_or_cut(_server):
    tools = {t.name: t for t in await _server.mcp.list_tools()}
    for name in TOOLS:
        tool = tools[name]
        shown = json.dumps(tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True))
        assert not _words(shown), (name, _words(shown))
        assert "sub-catchment" in tool.description
    source = (Path(__file__).resolve().parent.parent / "hydrata_mcp" / "catchment_network.py").read_text()
    sentences = re.findall(r'"([^"\n]{12,})"', source)
    leaks = [s for s in sentences if _words(s) and "/" not in s]
    assert not leaks, leaks


@respx.mock
async def test_a_terrain_preview_the_web_server_cannot_work_out_is_said_and_the_proposal_still_waits(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_dragged()))
    too_large = "This terrain is too large to work its drainage out again on the web server. Nothing was changed."
    respx.post(f"{SET}drags/ab12/terrain/").mock(
        return_value=httpx.Response(409, json={"error_code": "TOO_LARGE", "detail": too_large})
    )
    arguments = {"project_id": 42, "sub_catchments_id": 7, "sub_catchment_ids": [11, 12], "line": LINE}
    data = _tool_json(
        await _call_as_caller(
            _server, "propose_sub_catchment_boundary", {**arguments, "reason": REASON, "preview_terrain": True}
        )
    )
    assert data["status"] == "waiting" and data["terrain_preview"]["error_code"] == "TOO_LARGE"
    assert f"The terrain preview could not be worked out: {too_large}" in data["say_to_user"]


# ---------------------------------------------------------------------------
# TASK-4487 (W11.5): culverts among the sub-catchments - each culvert's area and flags in the read, and the one action for
# every flagged culvert's opening, which an agent only PROPOSES (D16): a person accepts the openings on the map.
# ---------------------------------------------------------------------------
OPENING = "Water reaching the inlet leaves 85 m from the outlet. An opening along the culvert would carry it through."
POND = "The pond behind the embankment takes in 12.5 ha, against 10.3 ha across the inlet. Check the culvert."
NO_OPENINGS = "No culvert of these sub-catchments has an opening to offer."
PROPOSED_TWO = "2 drainage openings proposed. A person accepts them on the map."


def _culverted(**version):
    """A set with two culverts at nodes (one offering an opening, one whose pond differs) and one not placed."""
    doc = _doc(**version)
    ref_a = {"culvert": 4, "feature": 2, "name": "Parker Rd", "status": "opening_offered", "flagged": True}
    ref_b = {"culvert": 4, "feature": 3, "name": "Northern Distributor", "status": "measured", "flagged": True}
    doc["version"]["nodes"].append(
        {
            "id": 33,
            "uid": "u-33",
            "kind": "culvert_inlet",
            "name": "",
            "lon": 149.12,
            "lat": -35.25,
            "level_m": 584.0,
            "line_catchment_id": 501,
            "source_ref": ref_a,
            "total_catchment": {},
        }
    )
    doc["version"]["nodes"].append(
        {
            "id": 34,
            "uid": "u-34",
            "kind": "culvert_inlet",
            "name": "",
            "lon": 149.13,
            "lat": -35.25,
            "level_m": 585.0,
            "line_catchment_id": 501,
            "source_ref": ref_b,
            "total_catchment": {},
        }
    )
    doc["version"]["culverts"] = {
        "count": 2,
        "flagged": 2,
        "openings_offered": 1,
        "placed": [
            {
                **ref_a,
                "node_kind": "culvert_inlet",
                "on_line": False,
                "area_m2": 12_000.0,
                "flags": [{"code": "opening_offered", "sentence": OPENING}],
                "opening_offered": True,
            },
            {
                **ref_b,
                "node_kind": "culvert_inlet",
                "on_line": False,
                "area_m2": 103_000.0,
                "flags": [{"code": "pond_differs", "sentence": POND}],
                "opening_offered": False,
            },
        ],
        "not_placed": [
            {"ref": {"culvert": 6, "feature": 1, "name": "Bellambi Ln"}, "sentence": "The culvert is off the terrain."}
        ],
    }
    return doc


@respx.mock
async def test_get_sub_catchments_reads_each_culvert_with_its_area_its_flags_and_its_node(_server):
    respx.get(SET).mock(return_value=httpx.Response(200, json=_culverted()))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    culverts = data["version"]["culverts"]
    assert culverts["count"] == 2 and culverts["flagged"] == 2 and culverts["openings_offered"] == 1
    assert culverts["placed"][0] == {
        "culvert": 4,
        "feature": 2,
        "name": "Parker Rd",
        "node_id": 33,
        "node_kind": "culvert_inlet",
        "on_line": False,
        "status": "opening_offered",
        "area_ha": 1.2,
        "flagged": True,
        "flags": [{"code": "opening_offered", "sentence": OPENING}],
        "opening_offered": True,
    }
    assert culverts["placed"][1]["node_id"] == 34 and culverts["placed"][1]["area_ha"] == 10.3
    assert culverts["not_placed"] == [
        {"culvert": 6, "feature": 1, "name": "Bellambi Ln", "sentence": "The culvert is off the terrain."}
    ]
    # a culvert's node names its culvert
    assert data["version"]["nodes"][2]["culvert"] == {"culvert": 4, "feature": 2}
    assert "culvert" not in data["version"]["nodes"][0]
    say = data["say_to_user"]
    assert "2 culverts are flagged; 1 offers a drainage opening along it" in say
    assert "a person accepts or rejects it on the map" in say
    assert not _words(json.dumps(data)), _words(json.dumps(data))


@respx.mock
async def test_get_sub_catchments_with_no_culverts_says_nothing_of_culverts(_server):
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    assert data["version"]["culverts"] == {
        "count": 0,
        "flagged": 0,
        "openings_offered": 0,
        "placed": [],
        "not_placed": [],
    }
    assert "culvert" not in data.get("say_to_user", "").lower()


@respx.mock
async def test_propose_culvert_openings_files_every_offered_opening_as_a_proposal_in_one_request(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_culverted()))
    answer = {
        "openings": [
            {
                "id": 71,
                "kind": "drainage_opening",
                "label": "Drainage opening",
                "status": "proposed",
                "note": "Opening along culvert 'Parker Rd'",
                "proposed_by_agent": True,
                "waiting_sentence": "Proposed. A person accepts it on the map.",
                "geometry": {"type": "LineString", "coordinates": [[1, 2], [3, 4]]},
            },
            {
                "id": 72,
                "kind": "drainage_opening",
                "label": "Drainage opening",
                "status": "proposed",
                "note": "Opening along culvert 'Lake Pde'",
                "proposed_by_agent": True,
                "waiting_sentence": "Proposed. A person accepts it on the map.",
                "geometry": {"type": "LineString", "coordinates": [[1, 2], [3, 4]]},
            },
        ],
        "stored": 2,
        "already_there": 0,
        "proposed": True,
        "drainage_analysis_job": None,
        "line_run": None,
        "sentence": PROPOSED_TWO,
    }
    post = respx.post(f"{SET}culvert-openings/").mock(return_value=httpx.Response(200, json=answer))
    data = _tool_json(
        await _call_as_caller(_server, "propose_culvert_openings", {"project_id": 42, "sub_catchments_id": 7})
    )
    assert post.call_count == 1  # ONE request for every opening offered
    assert json.loads(post.calls[0].request.content) == {}  # no picks: every opening offered
    assert data["outcome"] == "proposed" and data["status"] == "waiting" and data["proposed"] == 2
    assert [o["id"] for o in data["openings"]] == [71, 72]
    assert data["drainage_analysis_job"] is None  # a proposal starts no job
    assert data["ui_url"].endswith(MAP_URL)
    say = data["say_to_user"]
    assert say.startswith(PROPOSED_TWO)
    assert "Nothing changes until a person accepts them on the map" in say
    assert "only a person" in data["rule"].lower()
    assert "geometry" not in json.dumps(data)  # shapes stay on the map
    assert not _words(json.dumps(data)), _words(json.dumps(data))


@respx.mock
async def test_propose_culvert_openings_sends_only_the_culverts_picked(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_culverted()))
    answer = {
        "openings": [{"id": 71, "status": "proposed"}],
        "stored": 1,
        "already_there": 0,
        "proposed": True,
        "drainage_analysis_job": None,
        "line_run": None,
        "sentence": "1 drainage opening proposed. A person accepts it on the map.",
    }
    post = respx.post(f"{SET}culvert-openings/").mock(return_value=httpx.Response(200, json=answer))
    data = _tool_json(
        await _call_as_caller(
            _server, "propose_culvert_openings", {"project_id": 42, "sub_catchments_id": 7, "culverts": [[4, 2]]}
        )
    )
    assert json.loads(post.calls[0].request.content) == {"culverts": [[4, 2]]}
    assert data["proposed"] == 1


async def test_propose_culvert_openings_refuses_a_wrong_culvert_pick_before_sending(_server):
    for wrong in ([], [[4]], [["four", 2]], [[4, 2, 1]]):
        text = _tool_error_text(
            await _call_as_caller(
                _server, "propose_culvert_openings", {"project_id": 42, "sub_catchments_id": 7, "culverts": wrong}
            )
        )
        assert "culverts" in text and "Nothing was sent" in text, (wrong, text)


@respx.mock
async def test_propose_culvert_openings_relays_the_refusal_when_no_culvert_offers_one(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_doc()))
    respx.post(f"{SET}culvert-openings/").mock(
        return_value=httpx.Response(409, json={"error_code": "NO_OPENINGS", "detail": NO_OPENINGS})
    )
    data = _tool_json(
        await _call_as_caller(_server, "propose_culvert_openings", {"project_id": 42, "sub_catchments_id": 7})
    )
    assert data["outcome"] == "refused" and data["error_code"] == "NO_OPENINGS"
    assert NO_OPENINGS in data["say_to_user"] and "Nothing changed." in data["say_to_user"]


@respx.mock
async def test_propose_culvert_openings_already_on_the_terrain_says_so(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_culverted()))
    respx.post(f"{SET}culvert-openings/").mock(
        return_value=httpx.Response(
            200,
            json={
                "openings": [],
                "stored": 0,
                "already_there": 1,
                "proposed": True,
                "drainage_analysis_job": None,
                "line_run": None,
            },
        )
    )
    data = _tool_json(
        await _call_as_caller(_server, "propose_culvert_openings", {"project_id": 42, "sub_catchments_id": 7})
    )
    assert data["outcome"] == "already_there" and data["proposed"] == 0 and data["already_there"] == 1
    assert "already on the terrain" in data["say_to_user"]


@respx.mock
async def test_edit_sub_catchments_takes_the_culverts_in(_server):
    _mock_project()
    changed = _culverted()
    changed["culverts_taken_in"] = {"added": ["Parker Rd"], "removed": [], "moved": [], "placed": 2, "flagged": 2}
    changed["changed"] = True
    post = respx.post(f"{SET}culverts/").mock(return_value=httpx.Response(200, json=changed))
    data = _tool_json(
        await _call_as_caller(
            _server, "edit_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "kind": "take_culverts_in"}
        )
    )
    assert post.called and json.loads(post.calls[0].request.content) == {}
    assert data["outcome"] == "changed" and data["culverts_taken_in"]["placed"] == 2
    assert data["version"]["culverts"]["flagged"] == 2
    assert data["say_to_user"].startswith("Took the culverts in: 2 placed (1 added), 2 flagged, on the draft")


# ---------------------------------------------------------------------------
# W12.5 (TASK-4598): diverted points, diversion links with their facts, label points, taking the diversions in
# ---------------------------------------------------------------------------
DIVERTED = {
    "id": 33,
    "uid": "u-33",
    "kind": "diverted_point",
    "name": "",
    "lon": 149.12,
    "lat": -35.262,
    "level_m": 582.4,
    "line_catchment_id": 502,
    "source_ref": None,
    "total_catchment": {"area": _fact(146_075.0, "m2", "All the ground that drains to the node.")},
}
DIVERSION_LINK = {
    "id": 52,
    "kind": "diversion",
    "from_node": 33,
    "to_node": 31,
    "from_outside": None,
    "to_outside": None,
    "line_link_id": 700,
    "geometry": {"type": "LineString", "coordinates": [[149.12, -35.262], [149.1, -35.26]]},
    "facts": {
        "arriving_area": _fact(146_075.0, "m2", "Plan area arriving at the upper end."),
        "length": _fact(212.0, "m", "Length along the line."),
        "fall": _fact(1.2, "m", "Level at the upper low point less the level at the receiving low point."),
        "slope": _fact(0.0057, "m/m", "Fall over length."),
        "drain_depth": _fact(0.8, "m", "Depth of a drain along the line."),
    },
}


def _diverted_doc(**version):
    doc = _doc(**version)
    shown = doc["version"]
    shown["nodes"].append(DIVERTED)
    shown["links"].append(DIVERSION_LINK)
    for sub, point in zip(shown["sub_catchments"], ([149.1512345678, -35.2512345678], [149.17, -35.27])):
        sub["label_point"] = point
    return doc


@respx.mock
async def test_get_sub_catchments_reads_a_diverted_point_its_diversion_link_facts_and_label_points(_server):
    respx.get(SET).mock(return_value=httpx.Response(200, json=_diverted_doc()))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    version = data["version"]
    point = next(n for n in version["nodes"] if n["id"] == 33)
    # a diverted point says where its water goes on to: the receiving drainage point (the diversion link's lower end)
    assert point["kind"] == "diverted_point" and point["diverted_to"] == 31
    assert all("diverted_to" not in n for n in version["nodes"] if n["kind"] != "diverted_point")
    link = next(k for k in version["links"] if k["id"] == 52)
    assert link == {
        "id": 52,
        "kind": "diversion",
        "from_node": 33,
        "to_node": 31,
        "from_outside": None,
        "to_outside": None,
        "line_link_id": 700,
        "facts": {"arriving_area": 146_075.0, "length": 212.0, "fall": 1.2, "slope": 0.0057, "drain_depth": 0.8},
    }
    assert data["fact_definitions"]["arriving_area"]["unit"] == "m2"
    assert data["fact_definitions"]["drain_depth"]["definition"] == "Depth of a drain along the line."
    # every sub-catchment's label point, inside its own piece, as [lon, lat] rounded to 6 places
    assert [s["label_point"] for s in version["sub_catchments"]] == [[149.151235, -35.251235], [149.17, -35.27]]
    assert "geometry" not in json.dumps(version["links"]) and "coordinates" not in json.dumps(data)
    assert not _words(json.dumps(data)), _words(json.dumps(data))


@respx.mock
async def test_get_sub_catchments_relays_diversions_changed_and_says_take_diversions_in(_server):
    doc = _diverted_doc(
        diversions_changed_since=True,
        diversions_changed_sentence="Diversions changed in step 2.",
        diversions_fix_sentence="Take the diversions in.",
        diversions_changed={"added": [[502, 501]], "removed": []},
        edits_not_made_again=[
            {"seq": 4, "kind": "merge", "label": "Merge two sub-catchments", "sentence": "Those two no longer meet."}
        ],
        hand_edits=[{"seq": 4, "kind": "merge", "label": "Merge two sub-catchments", "made_by_agent": False}],
        terrain_agree={"limit_cells": 250_000, "cells": 80_000, "too_large": False, "sentence": "Up to 250,000 cells."},
    )
    respx.get(SET).mock(return_value=httpx.Response(200, json=doc))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    version = data["version"]
    assert version["diversions_changed_since"] is True
    assert version["diversions_changed"] == {"added": [[502, 501]], "removed": []}
    assert version["edits_not_made_again"] == [
        {"seq": 4, "kind": "merge", "label": "Merge two sub-catchments", "sentence": "Those two no longer meet."}
    ]
    assert version["terrain_agree"] == {
        "limit_cells": 250_000,
        "cells": 80_000,
        "too_large": False,
        "sentence": "Up to 250,000 cells.",
    }
    say = data["say_to_user"]
    assert "Diversions changed in step 2. Take the diversions in." in say
    assert "Merge two sub-catchments could not be made again: Those two no longer meet." in say
    # never a blocker: the read says nothing of holding the confirm
    assert "cannot be confirmed" not in say


def _taken_in(changed=True, preview=True, not_made=True):
    doc = _diverted_doc()
    doc["diversions_taken_in"] = {
        "changed": changed,
        "preview": preview,
        "sentence": (
            "1 edit is made again. 1 edit cannot be made again. It stays in the list, to look at."
            if changed
            else "These sub-catchments were created with the diversions as they are. There is nothing to take in."
        ),
        "replayed": [{"seq": 3, "kind": "divide", "label": "Divide a sub-catchment", "made_by_agent": True}]
        if changed
        else [],
        "not_made_again": [
            {
                "seq": 4,
                "kind": "merge",
                "label": "Merge two sub-catchments",
                "made_by_agent": False,
                "sentence": "Those two no longer meet.",
            }
        ]
        if changed and not_made
        else [],
        "diversions_changed": {"added": [[502, 501]], "removed": []} if changed else None,
        **({"version": 2} if changed else {}),
    }
    return doc


@respx.mock
async def test_edit_sub_catchments_take_diversions_in_previews_first_and_stores_nothing(_server):
    _mock_project()
    post = respx.post(f"{SET}diversions/").mock(return_value=httpx.Response(200, json=_taken_in()))
    data = _tool_json(
        await _call_as_caller(
            _server, "edit_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "kind": "take_diversions_in"}
        )
    )
    assert json.loads(post.calls[0].request.content) == {"preview": True}
    assert data["outcome"] == "preview"
    taken = data["diversions_taken_in"]
    assert taken["replayed"] == [{"seq": 3, "kind": "divide", "label": "Divide a sub-catchment", "made_by_agent": True}]
    assert taken["not_made_again"] == [
        {
            "seq": 4,
            "kind": "merge",
            "label": "Merge two sub-catchments",
            "made_by_agent": False,
            "sentence": "Those two no longer meet.",
        }
    ]
    assert taken["diversions_changed"] == {"added": [[502, 501]], "removed": []}
    say = data["say_to_user"]
    assert "1 edit is made again. 1 edit cannot be made again." in say and "Nothing was stored." in say
    assert "preview false" in data["next_step"]
    assert data["ui_url"].endswith(MAP_URL)
    assert not _words(json.dumps(data)), _words(json.dumps(data))


@respx.mock
async def test_edit_sub_catchments_take_diversions_in_with_preview_false_takes_them_in(_server):
    _mock_project()
    post = respx.post(f"{SET}diversions/").mock(
        return_value=httpx.Response(200, json=_taken_in(preview=False, not_made=False))
    )
    data = _tool_json(
        await _call_as_caller(
            _server,
            "edit_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "kind": "take_diversions_in", "preview": False},
        )
    )
    assert json.loads(post.calls[0].request.content) == {"preview": False}
    assert data["outcome"] == "changed" and data["version"]["number"] == 1
    assert data["say_to_user"].startswith(
        "Took the diversions in on the draft of the sub-catchments of Kaleen toe (version 2): 1 edit is made again."
    )
    assert data["rule"].startswith("Sub-catchments are a draft until a person confirms them")


@respx.mock
async def test_edit_sub_catchments_take_diversions_in_with_nothing_to_take_in_says_so(_server):
    _mock_project()
    respx.post(f"{SET}diversions/").mock(return_value=httpx.Response(200, json=_taken_in(changed=False)))
    data = _tool_json(
        await _call_as_caller(
            _server, "edit_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "kind": "take_diversions_in"}
        )
    )
    assert data["outcome"] == "unchanged"
    assert data["say_to_user"].startswith("These sub-catchments were created with the diversions as they are.")


@respx.mock
async def test_edit_sub_catchments_take_diversions_in_refused_after_the_line_ran_again(_server):
    _mock_project()
    respx.post(f"{SET}diversions/").mock(
        return_value=httpx.Response(
            409,
            json={
                "error_code": "LINE_CHANGED",
                "detail": "The line has run again since these were created. Create again from the new result.",
                "fix_sentence": "Create again from the new result.",
            },
        )
    )
    data = _tool_json(
        await _call_as_caller(
            _server, "edit_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "kind": "take_diversions_in"}
        )
    )
    assert data["outcome"] == "refused" and data["error_code"] == "LINE_CHANGED"
    assert data["say_to_user"].startswith("Refused taking the diversions in: The line has run again")
    assert data["say_to_user"].endswith("Nothing changed.")


@respx.mock
async def test_create_sub_catchments_estimate_only_answers_what_create_makes_and_stores_nothing(_server):
    # epic 4674 W4 (TASK-4697): the estimate of a divide by a maximum area is its preview
    _mock_project()
    preview = {
        "max_area_ha": 4_600.0,
        "suggested_max_area_ha": 4_600.0,
        "catchment_count": 38,
        "over_count": 4,
        "about_count": 62,
        "sentence": "4 of 38 catchments divided · about 62 sub-catchments",
        "over": [{"catchment_id": 501, "name": "Rail Ch 1200", "chainage_m": 1200.0, "area_ha": 92_495.0, "about": 21}],
    }
    post = respx.post(LIST).mock(return_value=httpx.Response(200, json={"preview": preview}))
    data = _tool_json(
        await _call_as_caller(
            _server,
            "create_sub_catchments",
            {"project_id": 42, "delineation_id": 9, "max_area_ha": 4600, "estimate_only": True},
        )
    )
    assert json.loads(post.calls[0].request.content) == {
        "delineation_id": 9,
        "max_area_ha": 4600.0,
        "estimate_only": True,
    }
    assert data["outcome"] == "preview" and data["preview"]["over"] == preview["over"]
    assert data["say_to_user"] == (
        "At a maximum of 4,600 ha: 4 of 38 catchments divided · about 62 sub-catchments. The suggested maximum is "
        "4,600 ha. Nothing was stored; ask the person for the maximum before dividing."
    )
    assert not _words(json.dumps(data))


async def _create_tool(srv) -> dict:
    tool = next(t for t in await srv.mcp.list_tools() if t.name == "create_sub_catchments")
    return tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)


async def test_max_area_ha_the_tool_schema_has_it_and_no_target_count_or_threshold(_server):
    schema = (await _create_tool(_server))["inputSchema"]["properties"]
    assert "max_area_ha" in schema and "target_count" not in schema and "threshold_m2" not in schema


async def test_max_area_ha_the_tool_description_says_divide_never_cut_or_split(_server):
    tool = await _create_tool(_server)
    words = " ".join(
        [tool.get("description", ""), *(p.get("description", "") for p in tool["inputSchema"]["properties"].values())]
    )
    assert "divide" in words.lower() and "estimate_only" in words
    assert "cut" not in words.lower().replace("shortcut", "") and "split" not in words.lower()
    assert "never choose the number" in words.lower()


# ---------------------------------------------------------------------------
# Epic 4674 W5.5 (TASK-4705): names in the agent tools
# ---------------------------------------------------------------------------


def _named_doc(provisional=True):
    doc = _doc()
    version = doc["version"]
    version["status"] = "draft" if provisional else "confirmed"
    a, b = version["sub_catchments"]
    a.update(uid="0b1c-a", name="CH5+081-01A", provisional=provisional, drains_to_name="Line", name_history=[])
    b.update(uid="0b1c-b", name="CH5+081-02A", provisional=provisional, drains_to_name="CH5+081-01A", name_history=[])
    a["notes"] = [{**NOTE, "other": 12}]
    changes = {"changed": [], "added": ["CH5+081-01A", "CH5+081-02A"], "retired": [], "first": True, "key": "k1"}
    version["names"] = {"form": "chainage", "provisional": provisional, "changes": changes if provisional else None}
    doc["chainage_names"] = True
    return doc


@respx.mock
async def test_subcatchment_name_read_carries_the_permanent_id_name_provisional_and_drains_to(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_named_doc()))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    a, b = data["version"]["sub_catchments"]
    assert (a["id"], a["name"], a["provisional"], a["drains_to"], a["drains_to_node"]) == (
        "0b1c-a",
        "CH5+081-01A",
        True,
        "Line",
        31,
    )
    assert (b["id"], b["drains_to"]) == ("0b1c-b", "CH5+081-01A")
    assert a["notes"][0]["other"] == "0b1c-b"  # the note names the other by its permanent ID
    names = data["version"]["names"]
    assert names["form"] == "chainage" and names["chainage_names"] is True and names["provisional"] is True
    assert names["changes"]["added"] == ["CH5+081-01A", "CH5+081-02A"]
    assert "The names are provisional until a person confirms." in data["say_to_user"]


@respx.mock
async def test_subcatchment_name_confirmed_names_are_not_provisional(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_named_doc(provisional=False)))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    assert not any(s["provisional"] for s in data["version"]["sub_catchments"])
    assert data["version"]["names"]["changes"] is None
    assert "provisional" not in (data.get("say_to_user") or "")


@respx.mock
async def test_subcatchment_name_an_old_set_keeps_its_numbers_and_says_divided(_server):
    _mock_project()
    doc = _doc()
    sentence = "These sub-catchments keep their numbers until they are divided by maximum area and confirmed."
    doc["version"]["names"] = None
    doc["version"]["names_sentence"] = sentence
    respx.get(SET).mock(return_value=httpx.Response(200, json=doc))
    data = _tool_json(await _call_as_caller(_server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7}))
    a = data["version"]["sub_catchments"][0]
    assert a["id"] == 11 and a["label"] == 1 and "name" not in a and a["drains_to"] == 31
    assert data["version"]["names"] is None
    assert sentence in data["say_to_user"] and not FORBIDDEN.search(sentence) and "split" not in sentence


@respx.mock
async def test_subcatchment_name_edits_and_outlines_act_on_the_permanent_id(_server):
    _mock_project()
    respx.get(SET).mock(return_value=httpx.Response(200, json=_named_doc()))
    post = respx.post(f"{SET}edits/").mock(return_value=httpx.Response(200, json=_edited("merge")))
    _tool_json(
        await _call_as_caller(
            _server,
            "edit_sub_catchments",
            {"project_id": 42, "sub_catchments_id": 7, "kind": "merge", "sub_catchment_ids": ["0b1c-a", "0b1c-b"]},
        )
    )
    assert json.loads(post.calls[0].request.content)["sub_catchment_ids"] == ["0b1c-a", "0b1c-b"]
    data = _tool_json(
        await _call_as_caller(
            _server, "get_sub_catchments", {"project_id": 42, "sub_catchments_id": 7, "outlines_of": ["0b1c-b"]}
        )
    )
    a, b = data["version"]["sub_catchments"]
    assert "outline" in b and "outline" not in a
