"""TASK-3962 (epic 3958 D3/D4) — sampled, size-capped argument samples (``kind=sample`` lines).

Test names carry ``arg_sample`` so the card's stored proof (``-k arg_sample``) selects them.
"""

import base64
import hashlib
import json
import os

import httpx
import pytest
import respx

from hydrata_mcp import telemetry
from tests.helpers import BASE, KEY_HEADERS, _asgi_client, _parse, _tools_call

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("env_vars")]  # scrub() late-imports the configured helpers

PRESIGNED = "https://bucket.s3.amazonaws.com/terrain/dem.tif?X-Amz-Signature=cafebabe&X-Amz-Credential=AKIAZZZ"
CANARY = "CANARY-TOKEN-91d2"


def _record(user_id=12, tool="write_input_layer", outcome="refused"):
    return {
        "ts": "2026-10-29T00:00:00.000+00:00",
        "request_id": "r1",
        "tool": tool,
        "outcome": outcome,
        "user_id": user_id,
        "username": "ann",
        "internal": False,
        "identity": f"user:{user_id}",
        "identity_source": "django",
    }


def _sample(arguments, **kw):
    sample = telemetry.build_sample(_record(**kw), arguments)
    assert len(telemetry._dump(sample)) <= telemetry.SAMPLE_MAX_BYTES
    return sample


def _lines(capsys, kind):
    out = capsys.readouterr().out
    return [r for r in (json.loads(x) for x in out.splitlines() if x.startswith("{")) if r.get("kind") == kind]


async def _call(srv, name, arguments=None, headers=None):
    async with _asgi_client(srv) as c:
        resp = await c.post("/", headers={**KEY_HEADERS, **(headers or {})}, json=_tools_call(name, arguments))
    return _parse(resp)["result"]


async def test_arg_sample_canary_token_and_presigned_url_never_appear():
    args = {
        "note": f"see {PRESIGNED} and hk_0123456789ab_{CANARY}",
        "Authorization": f"Bearer {CANARY}",
        "api_key": CANARY,
        "nested": {"access_token": CANARY, "ok": 1},
        "s3_package_url": PRESIGNED,
    }
    text = json.dumps(_sample(args))
    for leak in (CANARY, "X-Amz-Signature", "cafebabe", "AKIAZZZ"):
        assert leak not in text, leak
    assert _sample(args)["arguments"]["nested"]["ok"] == 1


async def test_arg_sample_long_series_keeps_fifty_points_and_the_total():
    rows = [{"timestamp": f"2020-01-01T00:{i % 60:02d}:00", "value": i} for i in range(20000)]
    sample = _sample({"data": {"rowData": rows}})
    out = sample["arguments"]["data"]["rowData"]
    assert len(out["items"]) == 50 and out["total"] == 20000
    assert out["items"][0]["value"] == 0 and out["items"][49]["value"] == 49


async def test_arg_sample_polygon_becomes_type_vertices_bbox():
    polygon = {"type": "Polygon", "coordinates": [[[1, 2], [5, 2], [5, 9], [1, 9], [1, 2]]]}
    assert _sample({"geometry": polygon})["arguments"]["geometry"] == {
        "type": "Polygon",
        "vertices": 5,
        "bbox": [1, 2, 5, 9],
    }
    multi = {
        "type": "MultiPolygon",
        "coordinates": [polygon["coordinates"], [[[10, 10], [11, 10], [11, 12], [10, 10]]]],
    }
    assert _sample({"g": multi})["arguments"]["g"]["vertices"] == 9


async def test_arg_sample_five_thousand_char_string_keeps_first_thousand_and_length():
    out = _sample({"description": "word " * 1000})["arguments"]["description"]
    assert out["length"] == 5000 and len(out["text"]) == 1000 and out["text"].startswith("word word")
    plain = _sample({"description": "x" * 5000})["arguments"]["description"]
    assert plain["length"] == 5000 and len(plain["text"]) == 1000  # low-diversity text is not mistaken for base64


async def test_arg_sample_base64_blob_keeps_type_size_sha256_only():
    raw = os.urandom(3000)
    blob = base64.b64encode(raw).decode()
    out = _sample({"file": blob, "uri": "data:image/png;base64," + blob})["arguments"]
    expected = {"type": "base64", "size": 3000, "sha256": hashlib.sha256(raw).hexdigest()}
    assert out["file"] == expected and out["uri"] == expected
    assert blob[:40] not in json.dumps(out)


async def test_arg_sample_feature_collection_of_200_keeps_fifty_with_total_and_fits_16kb():
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[i, i], [i + 1, i], [i + 1, i + 1], [i, i]]]},
            "properties": {"name": f"building {i}", "note": "n" * 400},
        }
        for i in range(200)
    ]
    sample = _sample({"project_id": 42, "kind": "building", "features": features})
    fc = sample["arguments"]["features"]
    assert fc["total"] == 200 and len(fc["items"]) <= 50
    assert fc["items"][0]["geometry"] == {"type": "Polygon", "vertices": 4, "bbox": [0, 0, 1, 1]}
    whole = {"type": "FeatureCollection", "features": features}
    kept = _sample({"geojson": whole})["arguments"]["geojson"]
    assert kept["features"]["total"] == 200 and kept["type"] == "FeatureCollection"


async def test_arg_sample_hard_ceiling_even_for_hostile_arguments():
    hostile = {f"k{i}": {"deep": ["y" * 900] * 60, "more": list(range(500))} for i in range(150)}
    sample = _sample(hostile)
    assert len(telemetry._dump(sample)) <= 16 * 1024
    unbounded = {"a": [{"b": "z" * 20_000} for _ in range(3)]}
    assert len(telemetry._dump(_sample(unbounded))) <= 16 * 1024


async def test_arg_sample_sampler_rules_cap_first_ok_and_refusals():
    sampler = telemetry.Sampler()
    me = "user:12"
    assert sampler.decide(me, "list_projects", "ok") is True  # first ok of the tool today
    assert sampler.decide(me, "list_projects", "ok") is False  # second ok, same tool
    assert sampler.decide(me, "list_projects", "refused") is True  # a refusal is sampled regardless
    assert sampler.decide(me, "get_project", "ok") is True  # another tool: its first ok
    assert sampler.decide("user:13", "list_projects", "ok") is True  # another user


async def test_arg_sample_fifty_first_sample_of_the_day_is_dropped_per_user():
    sampler = telemetry.Sampler()
    results = [sampler.decide("user:1", "t", "refused") for _ in range(51)]
    assert results[:50] == [True] * 50 and results[50] is False
    assert sampler.decide("user:2", "t", "refused") is True
    assert sampler.decide("user:1", "t", "broken") is False  # a refused call after the cap is also dropped


async def test_arg_sample_resets_on_a_new_day():
    day = ["2026-10-29"]
    sampler = telemetry.Sampler(day=lambda: day[0])
    for _ in range(50):
        sampler.decide("user:1", "t", "refused")
    assert sampler.decide("user:1", "t", "refused") is False
    day[0] = "2026-10-30"
    assert sampler.decide("user:1", "t", "refused") is True


async def test_arg_sample_identity_table_is_bounded():
    sampler = telemetry.Sampler()
    for i in range(telemetry.SAMPLE_MAX_IDENTITIES):
        sampler.decide(f"fp:{i}", "t", "refused")
    assert sampler.decide("fp:new", "t", "refused") is False


async def test_arg_sample_always_sampled_tools_ignore_the_cap():
    sampler = telemetry.Sampler()
    for _ in range(60):
        sampler.decide("user:1", "t", "refused")
    assert sampler.decide("user:1", "suggest_capability", "ok") is True
    assert sampler.decide("user:1", "suggest_capability", "ok") is True
    assert sampler.decide("user:1", "t", "refused") is False  # and they did not use up the count


@respx.mock
async def test_arg_sample_line_sits_next_to_the_call_record_with_the_same_request_id(_server, capsys):
    respx.get(f"{BASE}/projects/42/").mock(
        return_value=httpx.Response(
            200,
            json={"id": 42},
            headers={"X-Hydrata-Actor": "user_id=12&username=ann&grant=3&credential=agent_key&internal=0"},
        )
    )
    await _call(_server, "get_project", {"project_id": 42}, headers={"X-Request-ID": "rid-1"})
    await _call(_server, "get_project", {"project_id": 42}, headers={"X-Request-ID": "rid-2"})
    out = capsys.readouterr().out
    rows = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
    kinds = [(r["kind"], r["request_id"]) for r in rows]
    assert kinds == [("call", "rid-1"), ("sample", "rid-1"), ("call", "rid-2")]  # second ok: no sample
    sample = rows[1]
    assert sample["arguments"] == {"project_id": 42} and sample["user_id"] == 12 and sample["username"] == "ann"


@respx.mock
async def test_arg_sample_refused_call_is_sampled_after_the_tools_ok_sample(_server, capsys):
    respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42}))
    await _call(_server, "get_project", {"project_id": 42})
    respx.get(f"{BASE}/projects/43/").mock(return_value=httpx.Response(404, json={"detail": "nope"}))
    await _call(_server, "get_project", {"project_id": 43})
    assert [s["outcome"] for s in _lines(capsys, "sample")] == ["ok", "refused"]


@respx.mock
async def test_arg_sample_invalid_call_is_sampled_with_credential_identity(_server, capsys):
    await _call(_server, "get_project", {"project_id": CANARY})
    (sample,) = _lines(capsys, "sample")
    assert sample["outcome"] == "invalid" and sample["identity_source"] == "credential"
    assert sample["user_id"] is None and sample["identity"] == "hk_0123456789ab"


@respx.mock
async def test_arg_sample_only_the_arguments_not_the_result(_server, capsys):
    respx.get(f"{BASE}/projects/42/").mock(return_value=httpx.Response(200, json={"id": 42, "name": "RESULT-ONLY-XYZ"}))
    await _call(_server, "get_project", {"project_id": 42})
    assert "RESULT-ONLY-XYZ" not in "".join(json.dumps(s) for s in _lines(capsys, "sample"))
