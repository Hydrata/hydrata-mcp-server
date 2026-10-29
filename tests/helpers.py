"""Shared constants and helpers for the Hydrata MCP server tests (split out of the old tests/test_server.py)."""

import contextlib
import importlib
import inspect
import json

import httpx
import pytest
import respx

BASE = "https://hydrata.example.com/api/v2/anuga"


# ---------------------------------------------------------------------------
# TASK-3166 (W0.1, epic 2467) — credential pass-through.
#
# These cases drive the REAL ASGI app (lifespan + httpx.ASGITransport) rather
# than calling the tool functions directly, so the auth middleware and the
# fastmcp request context are both exercised. respx mocks only the server's
# OUTBOUND httpx client, so `route.calls[0].request.headers` is exactly what
# went upstream. Every case lives in TestPassthrough so the card's stored
# proof (`pytest -k passthrough`) selects all of them.
# ---------------------------------------------------------------------------
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",  # 406 without both
    "Content-Type": "application/json",
}
# TASK-3508 (W2a, epic 3200, D25): the caller's credential is an hk_ agent key,
# verified by Django; the MCP server forwards it verbatim and never parses it.
CALLER_KEY = "Bearer hk_0123456789ab_c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3JldA"
# foo:bar — a password caller, refused on /mcp/ tools/call since TASK-3730.
CALLER_BASIC = "Basic Zm9vOmJhcg=="
UPSTREAM_OK = {"count": 0, "results": []}
# TASK-3987: every method needs a credential, so a catalog fetch (initialize,
# tools/list) carries the key too.
KEY_HEADERS = {**MCP_HEADERS, "Authorization": CALLER_KEY}
INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "0"},
}
EVERY_METHOD = [
    pytest.param("initialize", INITIALIZE_PARAMS, id="initialize"),
    pytest.param("tools/list", {}, id="tools-list"),
    pytest.param("ping", {}, id="ping"),
]


def _rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


def _tools_call(name="list_projects", arguments=None, id=1):
    return _rpc("tools/call", {"name": name, "arguments": arguments or {}}, id=id)


def _parse(resp):
    """Pull the JSON-RPC payload out of an SSE (200) or plain-JSON (401) response."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :])
        raise AssertionError(f"no data: line in SSE body: {resp.text!r}")
    return resp.json()


@contextlib.asynccontextmanager
async def _asgi_client(srv):
    """Run the app's lifespan (else every request 500s) and yield a client bound to it."""
    async with srv.app.router.lifespan_context(srv.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=srv.app), base_url="http://testserver") as c:
            yield c


# ---------------------------------------------------------------------------
# TASK-2469 (W1.1, epic 2467) — project + terrain import tools.
#
# create_project / presign_terrain_upload / finalize_terrain_upload / get_terrain
# wrap the existing V2 REST API. Every case drives the tool through the REAL
# ASGI app with CALLER_KEY (see _asgi_client above) and asserts the caller's
# Authorization header reached the upstream mock — the pass-through is the
# whole point of the internal-first design. Request BODIES are asserted too:
# the REST contract (api_v2.py upload_presign :1651 / upload_finalize :1756,
# ProjectCreateSerializerV2 name+projection) is what the tools must speak.
# The stored proof selects on `-k "create_project or terrain"`, so every test
# NAME below carries one of those literal tokens (a class name does not match).
# ---------------------------------------------------------------------------
DEM_NAME = "towradgi_dem_1m_ahd.tif"


STAGING_KEY = f"terrain_uploads/staging/0f4b8c2e-1111-4222-8333-444455556666/{DEM_NAME}"
PROCESS_ID = "9a1b2c3d-4444-4555-8666-777788889999"


async def _call_as_caller(srv, name, arguments):
    """Drive `name` through the ASGI app as CALLER_KEY; return the JSON-RPC result."""
    async with _asgi_client(srv) as c:
        resp = await c.post(
            "/",
            headers={**MCP_HEADERS, "Authorization": CALLER_KEY},
            json=_tools_call(name, arguments),
        )
    assert resp.status_code == 200, resp.text
    return _parse(resp)["result"]


def _unwrapped(value):
    """`value` with TASK-3511's data envelope taken off: the markers and the leading note.

    The tools/call tests in this file pin what each tool RETURNS (its logic);
    tests/test_data_envelope.py pins the envelope itself on the raw text, for
    every tool. This keeps the two concerns apart instead of restating the
    markers in a hundred unrelated assertions.
    """
    if isinstance(value, dict):
        return {_unwrapped(k): _unwrapped(v) for k, v in value.items() if k != "untrusted_data_note"}
    if isinstance(value, list):
        return [_unwrapped(item) for item in value]
    if isinstance(value, str):
        return value.replace("<untrusted-data>", "").replace("</untrusted-data>", "")
    return value


def _tool_json(result):
    """The tool's text payload, parsed and unwrapped. Fails loudly on isError so the message shows."""
    assert result.get("isError") is not True, result["content"][0]["text"]
    return _unwrapped(json.loads(result["content"][0]["text"]))


HAND_BACK_KEYS = {"ui_url", "summary", "say_to_user", "hand_back_error"}


def _terrain(pk, status, **extra):
    return {"id": pk, "title": "mcp-w1-2469-dem", "status": status, "gn_layer": None, **extra}


# ---------------------------------------------------------------------------
# TASK-3171 (W1.2, epic 2467) — create_time_series + attach_input_layer.
#
# create_time_series POSTs /projects/<id>/time-series/ with `series_type` and
# `units` as TOP-LEVEL keys (TimeSeries HAS both fields: hydrology/models.py:90-96;
# the Hydrographs panel filters the list by ?series_type, so a folded-into-
# description value would leave an inflow series invisible) and mirrors
# TimeSeries.clean() client-side, because a bad row is an UNHANDLED 500 on the
# API (full_clean's ValidationError is not an APIException), whose body the
# client masks. attach_input_layer PATCHes the project's DEFAULT row for all
# SIX kinds — the terrain chain seeds Building 01 / MeshRegion 01 too, and a
# POST to /buildings/ or /mesh-regions/ with gn_layer is silently overwritten
# by the async layer factory (api_v2.py perform_create :5247-5261) — so the
# suite registers those POST routes and asserts they are NEVER called. The
# execution-status route is GeoNode's, outside /api/v2/anuga, so its URL is
# asserted at the ORIGIN. The stored proof selects on
# `-k "time_series or attach_input_layer"`: every test NAME carries a token.
# ---------------------------------------------------------------------------
ORIGIN = "https://hydrata.example.com"
EXEC_ID = "b7e1c2d3-4444-4555-8666-777788889999"
EXEC_STATUS_URL = f"{ORIGIN}/api/v2/resource-service/execution-status/{EXEC_ID}"


ROW_DATA = [
    {"timestamp": "1998-08-17T00:00:00", "value": 0.0},
    {"timestamp": "1998-08-17T00:05:00Z", "value": 1.5},  # clean() strips a trailing Z
    {"timestamp": "1998-08-17T00:10:00", "value": "2.25"},  # clean() float()s a numeric string
]


def _series_args(**overrides):
    args = {
        "project_id": 42,
        "name": "rain_gauge_200",
        "data": {"rowData": ROW_DATA},
        "series_type": "hyetograph",
        "units": "mm/hr",
        "timezone": "Australia/Sydney",
    }
    args.update(overrides)
    return args


def _series_row(pk, **overrides):
    row = {
        "id": pk,
        "project": 42,
        "name": "rain_gauge_200",
        "source": "",
        "description": "",
        "location_name": "",
        "timezone": "Australia/Sydney",
        "series_type": "hyetograph",
        "units": "mm/hr",
        "data": {"rowData": ROW_DATA},
        "perms": ["view", "change"],
    }
    row.update(overrides)
    return row


def _exec(status, resources=None, **extra):
    return {
        "user": "testuser",
        "status": status,
        "func_name": "import_new_resource",
        "output_params": {"resources": resources or []},
        "log": None,
        **extra,
    }


def _row(pk, title, gn_layer=None):
    return {"id": pk, "title": title, "project": 42, "gn_layer": gn_layer, "gn_layer_name": None, "perms": []}


def _dataset(pk, alternate):
    return {"dataset": {"pk": str(pk), "alternate": alternate, "title": alternate}}


def _tool_error_text(result):
    """The refusal text of an isError result. Fails loudly if the tool did NOT refuse."""
    assert result.get("isError") is True, result["content"][0]["text"]
    return result["content"][0]["text"]


SUMMARY_HYDROGRAPH = {
    "n_rows": 4,
    "first_at": "2020-01-01T00:00:00+00:00",
    "last_at": "2020-01-01T06:00:00+00:00",
    "duration_s": 21600.0,
    "peak_value": 19.7,
    "peak_at": "2020-01-01T02:00:00+00:00",
    "median_step_s": 7200.0,
    "min_step_s": 7200.0,
    "max_step_s": 7200.0,
    "total": 120000.0,
    "total_units": "m3",
    "n_negative": 0,
    "trailing_value": 0.0,
}


# ---------------------------------------------------------------------------
# TASK-3172 (W1.3, epic 2467) — create_scenario + build_scenario.
#
# create_scenario POSTs the WRITE serializer's fields (ScenarioCreateSerializerV2,
# serializers_v2.py:819 — its response carries NO estimate) and then GETs the
# detail, whose READ serializer carries mesh_triangle_count_estimate (+ the
# _breakdown) and `computed_status` — NEVER `status`: the round-2 red-team found
# the pre-existing get_scenario test mocking a key the API does not return.
# build_scenario shows the number before it spends: the re-call checks on
# latest_run come FIRST (a re-POST after `built` dispatches a NEW Run,
# services.py:1042-1048; the dedup 409 blocks only in-flight + created), then
# the boundary / resolution / 100k-confirm gates, then POST /build/ with
# raise_for_status=False so a 422 MESH_TOO_LARGE (or a 409) body reaches the
# agent VERBATIM as a non-error result, then a bounded poll of the detail's
# computed_status. Every case asserts whether /build/ was hit. The stored proof
# selects on `-k "create_scenario or build_scenario"`: every test NAME carries
# one of those tokens.
# ---------------------------------------------------------------------------
SCENARIO_URL = f"{BASE}/projects/42/scenarios/5/"
BUILD_URL = f"{BASE}/projects/42/scenarios/5/build/"
BOUNDARY_URL = f"{BASE}/projects/42/boundaries/27497/"
ESTIMATE_BREAKDOWN = {"base": 19_729, "mesh_regions": 0, "buildings": 0, "total": 19_729}


def _run(pk, status, **extra):
    return {
        "id": pk,
        "project": 42,
        "scenario": 5,
        "status": status,
        "error_message": None,
        "user_message": None,
        "status_detail": None,
        "mesh_triangle_count": None,
        "log": "…the full build log…",
        **extra,
    }


def _scenario(computed_status="created", estimate=19_729, latest_run=None, **overrides):
    """A member's ScenarioSerializerV2 detail: boundary + computed_status present."""
    row = {
        "id": 5,
        "project": 42,
        "name": "mcp-w1-3172-t",
        "description": "",
        "boundary": 27497,
        "terrain": 110533,
        "friction": 124557,
        "inflow": 124555,
        "rainfall": 124556,
        "building": 124558,
        "mesh_region": None,
        "network": None,
        "resolution": 36.0,
        "duration": 43200,
        "computed_status": computed_status,
        "latest_run": latest_run,
        "latest_complete_run": None,
        "mesh_triangle_count_estimate": estimate,
        "mesh_triangle_count_estimate_breakdown": (
            None if estimate is None else {**ESTIMATE_BREAKDOWN, "total": estimate}
        ),
        "latest_run_is_valid": None,
        "perms": ["view", "change"],
    }
    row.update(overrides)
    return row


def _mock_boundary(has_features=True):
    return respx.get(BOUNDARY_URL).mock(
        return_value=httpx.Response(
            200,
            json={"id": 27497, "title": "Boundary 01", "project": 42, "gn_layer": 1524, "has_features": has_features},
        )
    )


def _mock_build(status=202, body=None):
    body = {"status": "created", "run_id": 501, "scenario_id": 5} if body is None else body
    return respx.post(BUILD_URL).mock(return_value=httpx.Response(status, json=body))


def _detail_to_built(first_detail):
    """Detail GETs: the pre-check read, then the poll sees created → building → built."""
    return respx.get(SCENARIO_URL).mock(
        side_effect=[
            httpx.Response(200, json=first_detail),
            httpx.Response(200, json=_scenario("created", latest_run=_run(501, "created"))),
            httpx.Response(200, json=_scenario("building", latest_run=_run(501, "building"))),
            httpx.Response(
                200,
                json=_scenario(
                    "built",
                    latest_run=_run(501, "built", mesh_triangle_count=19_812),
                    latest_run_is_valid=True,
                ),
            ),
        ]
    )


async def _build(srv, **overrides):
    args = {"project_id": 42, "scenario_id": 5, "poll_interval_seconds": 0}
    args.update(overrides)
    return await _call_as_caller(srv, "build_scenario", args)


# ---------------------------------------------------------------------------
# TASK-3187 (W0, epic 3200) — presigned-URL elision + the compact get_scenario.
#
# A remote MCP with credential pass-through must not widen what a transcript
# can leak beyond what the caller asked for. A live scenario detail carries TEN
# presigned S3 URLs (five `s3_*_url` keys on `latest_run` and five more on
# `latest_complete_run`), each a 6-hour capability with an STS token; the read
# tools relayed them verbatim into the agent transcript. One recursive helper
# elides them for every read tool + start_simulation; `presign_terrain_upload`
# is the SOLE documented exception (the caller asked for that URL).
#
# The stored proof selects on `-k "presigned or elide or get_scenario"`, so
# every test NAME below carries one of those LITERAL tokens — `pytest -k` is
# case-sensitive and does not match a class name, so `TestGetScenario` is not
# a selection (that is why the proof was RED at HEAD: `116 deselected`).
# ---------------------------------------------------------------------------
# A fake SigV4 query with all six X-Amz-* params a real presigned URL carries:
# redacting only the matched param would leave X-Amz-Credential behind.
SIGNED_QUERY = (
    "X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=FAKEKEYIDNOTREAL%2F20260922%2Fus-west-2%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260922T000000Z&X-Amz-Expires=21600&X-Amz-SignedHeaders=host"
    "&X-Amz-Security-Token=fake-sts-token-for-tests&X-Amz-Signature=deadbeefdeadbeef"
)
# What an elided value (or an elided substring of a log line) reads as.
ELIDED = "[presigned-url-elided]"
# The five signed keys on a run record (serializers_v2.py RunSerializerV2).
RUN_S3_URL_KEYS = (
    "s3_package_url",
    "s3_result_package_url",
    "s3_depth_max_url",
    "s3_velocity_max_url",
    "s3_depth_integrated_velocity_max_url",
)
# A run's WMS dict, shaped like the live one on run 67181: its NAME matches
# neither `*_url` nor `s3_*`, but it NESTS a `url` (GeoServer OWS) and a
# `catalogURL` (catalogue CSW) that a case-insensitive "url in key" rule eats.
GN_LAYER_DEPTH_MAX = {
    "pk": 1538,
    "name": "dep_67181_depth_max",
    "title": "Depth max",
    "alternate": "geonode:dep_67181_depth_max",
    "url": "http://localhost:8080/geoserver/ows",
    "catalogURL": (
        "http://localhost:8081/catalogue/csw?outputschema="
        "http%3A%2F%2Fwww.isotc211.org%2F2005%2Fgmd&service=CSW&request=GetRecordById"
        "&version=2.0.2&elementsetname=full&id=dep-67181-depth-max"
    ),
    "visibility": True,
}


def _signed_url(key):
    return f"https://anuga-test-storage.s3.amazonaws.com/{key}?{SIGNED_QUERY}"


def _presigned_run(pk=67181, status="complete", **extra):
    """A run record as RunSerializerV2 serialises it: five signed `s3_*_url`
    keys, the WMS dicts, and a build log."""
    run = _run(pk, status, **extra)
    run.update({key: _signed_url(f"runs/{pk}/{key}") for key in RUN_S3_URL_KEYS})
    run["gn_layer_depth_max"] = dict(GN_LAYER_DEPTH_MAX)
    return run


# ---------------------------------------------------------------------------
# TASK-3203 (W1, epic 3200) — the hand-back: ui_url + summary + say_to_user on
# every mutating tool, refusals that route the human to the map, and the
# informational X-Hydrata-Client / X-Hydrata-Session headers.
#
# The stored proof selects on `-k "ui_url or say_to_user or client_header"`, so
# every test NAME below carries one of those literal tokens.
# ---------------------------------------------------------------------------
UI_URL_RE = r"^https?://.+/catalogue/#/map/\d+"
BASE_MAP = 1525
BOUNDARY_ALTERNATE = "geonode:bdy_42_boundary_01"


def _gs_tripwire(server, monkeypatch):
    """TASK-3749: a transport that fails on any map-server (/gs/) URL, and records it.

    Since TASK-3728 Hydrata refuses /gs/ for every agent key, and since TASK-3730
    every tools/call carries one, so the MCP must never ask /gs/ anything. Every
    request goes through HydrataClient._send; this wraps it. A /gs/ URL is recorded
    and raises; tests assert the record is EMPTY, because a best-effort caller
    could swallow the raise.
    """
    hits: list = []
    original = server.client._send

    async def send(method, url, path, *args, **kwargs):
        if "/gs/" in url or path.startswith("/gs/"):
            hits.append(url)
            raise AssertionError(f"TASK-3749: the MCP made a /gs/ request ({url})")
        return await original(method, url, path, *args, **kwargs)

    monkeypatch.setattr(server.client, "_send", send)
    return hits


def _mock_project(base_map=BASE_MAP):
    return respx.get(f"{BASE}/projects/42/").mock(
        return_value=httpx.Response(200, json={"id": 42, "name": "p", "base_map": base_map})
    )


def _assert_hand_back(data, **params):
    import re as _re

    assert _re.match(UI_URL_RE, data["ui_url"] or ""), data.get("ui_url")
    assert f"/catalogue/#/map/{BASE_MAP}" in data["ui_url"]
    for key, value in params.items():
        assert f"{key}={value}" in data["ui_url"], (key, value, data["ui_url"])
    assert isinstance(data["summary"], dict) and data["summary"], data["summary"]
    assert isinstance(data["say_to_user"], str) and data["say_to_user"].strip()


def _mock_layer_summary(alternate, on_map=True):
    respx.get(url__startswith=f"{ORIGIN}/api/v2/datasets/?").mock(
        return_value=httpx.Response(
            200,
            json={
                "datasets": [
                    {
                        "alternate": alternate,
                        "ll_bbox_polygon": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [150.89, -34.38],
                                    [150.89, -34.37],
                                    [150.91, -34.37],
                                    [150.91, -34.38],
                                    [150.89, -34.38],
                                ]
                            ],
                        },
                    }
                ]
            },
        )
    )
    respx.get(url__startswith=f"{ORIGIN}/api/v2/maps/{BASE_MAP}/").mock(
        return_value=httpx.Response(
            200,
            json={
                "map": {
                    "maplayers": [
                        {"name": alternate if on_map else "geonode:bdy_42_boundary_old"},
                    ]
                }
            },
        )
    )


# ---------------------------------------------------------------------------
# TASK-3449 (W1b, epic 3200) — structuredContent on every tool. Each tool's
# registered wrapper (not the module function the direct-call tests use) hands
# back the parsed JSON text as structuredContent, and tools/list advertises an
# object outputSchema to match. Stored proof: -k structured_content.
# ---------------------------------------------------------------------------
def _schema_args(schema):
    """Minimal valid arguments for a tool's inputSchema (required params only)."""

    def value(prop):
        types = [prop.get("type")] + [p.get("type") for p in prop.get("anyOf", [])]
        if "integer" in types:
            return 1
        if "number" in types:
            return 0.01
        if "boolean" in types:
            return False
        if "object" in types:
            return {}
        if "array" in types:
            return []
        return "x"

    props = schema.get("properties", {})
    args = {name: value(props[name]) for name in schema.get("required", [])}
    for poll_knob, knob in (("timeout_seconds", 0), ("poll_interval_seconds", 0.01)):
        if poll_knob in props:
            args[poll_knob] = knob  # the polling tools return after one read
    return args


async def _call(srv, auth, name="list_projects", arguments=None):
    async with _asgi_client(srv) as c:
        return await c.post("/", headers={**MCP_HEADERS, "Authorization": auth}, json=_tools_call(name, arguments))


# A GeoNode OAuth2 access token (30 alnum chars, the shape django-oauth-toolkit
# issues) — what the /gs/ blocker (TASK-3728) let an agent harvest.
CALLER_OAUTH = "Bearer 7fQ2xLm9RtY4vW1zKp8sNd3hJc6bGa"
# 'Basic base64(user:hk_…)': a key smuggled inside Basic must not count as one.
CALLER_BASIC_HOLDING_KEY = "Basic dXNlcjpoa18wMTIzNDU2Nzg5YWJfYzJWamNtVjA="


# ---------------------------------------------------------------------------
# Reloading the server. `Config.from_env()` and the HydrataClient are built at
# import, so a test that sets env vars reloads the server. The server is now a
# package of modules (`_common` owns config/client/mcp, the tool families
# register on it at import), so every one of them reloads, dependencies first.
# This is the order server.py imports them in (= the tools/list order).
# ---------------------------------------------------------------------------
SERVER_MODULES = (
    "_common",
    "_helpers",
    "projects",
    "runs",
    "terrain",
    "inputs",
    "observations",
    "calibration",
    "scenarios",
    "server",
)


class ServerModules:
    """Read-only view over the server modules: ``_server.get_scenario`` finds the name wherever it now lives.

    Reads only: to patch a module global, patch it on the module that USES it
    (``monkeypatch.setattr("hydrata_mcp.inputs._poll_until", ...)``).
    """

    def __getattr__(self, name):
        for module_name in reversed(SERVER_MODULES):
            module = importlib.import_module(f"hydrata_mcp.{module_name}")
            if name in vars(module):
                return vars(module)[name]
        raise AttributeError(name)

    def module_source(self):
        """The source text of every server module, for tests that assert a string is nowhere in the code."""
        return "\n".join(
            inspect.getsource(importlib.import_module(f"hydrata_mcp.{module_name}")) for module_name in SERVER_MODULES
        )

    def __setattr__(self, name, value):
        raise AttributeError(f"patch {name!r} on the module that uses it, not on this view")


def reload_server():
    """Reload every server module (so env vars are re-read) and return the view over them."""
    for module_name in SERVER_MODULES:
        importlib.reload(importlib.import_module(f"hydrata_mcp.{module_name}"))
    return ServerModules()
