"""TASK-4204 (epic 4117 W7h, GATE 4121): a sign-in token that ages out during a long call is not a revocation.

A tool call keeps the bearer it started with. Live on hydrata.com (05-11-26) a get_run_status wait and a
get_terrain wait outlived their WorkOS token; Django refused the next poll (ExpiredSignatureError) as
AGENT_KEY_INVALID, whose text says 'revoked ... Connect again', and the agent told the person to reconnect.

* R2: every wait reads the bearer's ``exp`` (unverified, timing only) and stops ``TOKEN_EXPIRY_MARGIN_SECONDS``
  before it, returning the tool's normal resumable "still waiting, call again" result; a token with no
  readable ``exp`` (an hk_ key, a non-JWT) is not capped.
* R3: if Django still answers 401 AGENT_TOKEN_EXPIRED the tool returns a resumable AGENT_TOKEN_EXPIRED result
  (call the same tool again, never ask the person to reconnect); the 'revoked ... Connect again' text is only
  ever Django's AGENT_KEY_INVALID body, relayed as before.

Every test name carries ``token_expired`` (the card's stored proof selects on it).
"""

import base64
import json
import time
import types

import httpx
import pytest
import respx

from tests.helpers import BASE, CALLER_KEY, _call, _parse, _tool_error_text, _tool_json, reload_server

pytestmark = pytest.mark.asyncio

RESOURCE = "https://hydrata.com/mcp/"


def _b64(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _jwt(**claims) -> str:
    """``Bearer header.payload.signature``: the shape only (this server never verifies a token)."""
    return f"Bearer {_b64({'alg': 'RS256', 'kid': 'k1'})}.{_b64({'sub': 'user_01', **claims})}.c2lnbmF0dXJl"


@pytest.fixture
def connect_server(env_vars, monkeypatch):
    monkeypatch.setenv("HYDRATA_MCP_RESOURCE_URL", RESOURCE)
    return reload_server()


@pytest.fixture
def fake_clock(connect_server, monkeypatch):
    """The polling loop's clock and sleep, faked on _helpers only: a 180 s wait takes no real time."""
    from hydrata_mcp import _helpers

    clock = types.SimpleNamespace(t=0.0)

    async def sleep(seconds):
        clock.t += seconds

    monkeypatch.setattr(_helpers, "time", types.SimpleNamespace(monotonic=lambda: clock.t))
    monkeypatch.setattr(_helpers, "asyncio", types.SimpleNamespace(sleep=sleep))
    return clock


def _computing():
    return httpx.Response(200, json={"id": 9, "status": "computing", "progress_pct": 20, "compute_backend": "batch"})


async def _result(srv, auth, name, arguments):
    resp = await _call(srv, auth, name, arguments)
    assert resp.status_code == 200, resp.text
    return _parse(resp)["result"]


EXPIRED_BODY = {
    "detail": (
        "This sign-in token expired while the call was in progress. Nothing was revoked and the connection "
        "is still live: make the same call again (your AI assistant sends a fresh token on its own)."
    ),
    "error_code": "AGENT_TOKEN_EXPIRED",
}
REVOKED_DETAIL = (
    "This connection is not valid: it was revoked on the Agent keys page, its sign-in expired or was "
    "issued for a different service, or its account is inactive. Connect again from your AI assistant."
)


# ---------------------------------------------------------------------------
# R2: the wait ends before the token does
# ---------------------------------------------------------------------------
class TestWaitCap:
    @respx.mock
    async def test_token_expired_cap_ends_a_get_run_status_wait_early_with_the_resumable_result(
        self, connect_server, fake_clock
    ):
        route = respx.get(f"{BASE}/runs/9/status/").mock(return_value=_computing())
        data = _tool_json(
            await _result(
                connect_server, _jwt(exp=int(time.time()) + 60), "get_run_status", {"run_id": 9, "wait_seconds": 180}
            )
        )
        # exp - now - 20 s: about 40 s of the 180 asked for, then the SAME result the 180 s ceiling gives.
        assert 38.0 <= data["waited_seconds"] <= 40.0 and 38.0 <= fake_clock.t <= 40.0
        assert data["timed_out"] is True and data["status"] == "computing"
        assert "call get_run_status again with wait_seconds=180" in data["next_step"]
        assert "error_code" not in data
        assert all(call.request.headers["authorization"].startswith("Bearer ") for call in route.calls)

    @respx.mock
    async def test_token_expired_cap_already_spent_does_one_read_and_no_wait(self, connect_server, fake_clock):
        route = respx.get(f"{BASE}/runs/9/status/").mock(return_value=_computing())
        data = _tool_json(
            await _result(
                connect_server, _jwt(exp=int(time.time()) + 10), "get_run_status", {"run_id": 9, "wait_seconds": 180}
            )
        )
        assert route.call_count == 1 and fake_clock.t == 0.0
        assert data["timed_out"] is True and data["waited_seconds"] == 0.0 and "next_step" in data

    @respx.mock
    async def test_token_expired_cap_already_spent_next_step_does_not_invite_an_instant_recall(
        self, connect_server, fake_clock
    ):
        """TASK-4223 AC1: the same call made at once would carry the same nearly-expired token and do the same
        zero-wait read, so the hand-back says to call again after the person's next message (or a pause)."""
        respx.get(f"{BASE}/runs/9/status/").mock(return_value=_computing())
        data = _tool_json(
            await _result(
                connect_server, _jwt(exp=int(time.time()) + 10), "get_run_status", {"run_id": 9, "wait_seconds": 180}
            )
        )
        step = data["next_step"]
        assert "call get_run_status again with wait_seconds=180" not in step
        assert "Do not call get_run_status again straight away" in step
        assert "next message" in step and "30 seconds" in step
        assert "reconnect" in step and "connect again" not in step.lower()
        assert data["token_refresh_pending"] is True

    @respx.mock
    async def test_token_expired_cap_partly_spent_keeps_the_normal_call_again_wording(self, connect_server, fake_clock):
        respx.get(f"{BASE}/runs/9/status/").mock(return_value=_computing())
        data = _tool_json(
            await _result(
                connect_server, _jwt(exp=int(time.time()) + 60), "get_run_status", {"run_id": 9, "wait_seconds": 180}
            )
        )
        assert "call get_run_status again with wait_seconds=180" in data["next_step"]
        assert "token_refresh_pending" not in data

    @respx.mock
    async def test_token_expired_transport_refusal_never_says_connect_again(self, connect_server):
        """TASK-4223 AC2: an already-expired connect token is refused at the transport with 401 invalid_token so the
        client refreshes; the words must not read like a revocation (only Django's AGENT_KEY_INVALID may)."""
        upstream = respx.route().mock(return_value=httpx.Response(200, json={}))
        resp = await _call(connect_server, _jwt(exp=int(time.time()) - 10), "list_projects", {})
        assert resp.status_code == 401 and not upstream.calls
        assert 'error="invalid_token"' in resp.headers["www-authenticate"]
        message = resp.json()["error"]["message"]
        assert "connect again" not in message.lower() and "revoked on" not in message
        assert "expired" in message and "refresh" in message
        # The transport reads exp unverified and knows nothing of the grant: it must not vouch for it (D72).
        assert "Nothing was revoked" not in message and "still live" not in message

    @pytest.mark.parametrize("age", [10, 600, 3590], ids=["10s", "10min", "just-inside-1h"])
    @respx.mock
    async def test_token_expired_within_the_hour_is_the_transport_401_the_client_refreshes_on(
        self, connect_server, age
    ):
        """TASK-4223 review fix (D72): a recently expired connect token still gets 401 invalid_token at the transport,
        so the OAuth client refreshes it; nothing reaches Django."""
        upstream = respx.route().mock(return_value=httpx.Response(200, json={}))
        resp = await _call(connect_server, _jwt(exp=int(time.time()) - age), "list_projects", {})
        assert resp.status_code == 401 and not upstream.calls
        assert 'error="invalid_token"' in resp.headers["www-authenticate"]
        assert "has expired: refresh it" in resp.json()["error"]["message"]

    @pytest.mark.parametrize("age", [3610, 2 * 3600, 365 * 86400], ids=["just-outside-1h", "2h", "1y"])
    @respx.mock
    async def test_token_expired_over_an_hour_ago_takes_the_normal_path_and_django_answers(self, connect_server, age):
        """TASK-4223 review fix (D72): past hydrata's 1 h window the transport says nothing of its own; the request
        goes the way an unexpired bearer goes and Django's AGENT_KEY_INVALID body is relayed byte for byte."""
        token = _jwt(exp=int(time.time()) - age)
        upstream = respx.get(f"{BASE}/projects/").mock(
            return_value=httpx.Response(401, json={"detail": REVOKED_DETAIL, "error_code": "AGENT_KEY_INVALID"})
        )
        resp = await _call(connect_server, token, "list_projects", {})
        assert resp.status_code == 200, resp.text
        assert upstream.calls and upstream.calls[0].request.headers["authorization"] == token
        text = _tool_error_text(_parse(resp)["result"])
        assert "AGENT_KEY_INVALID" in text and REVOKED_DETAIL in text
        assert "has expired: refresh it" not in text and "AGENT_TOKEN_EXPIRED" not in text

    @respx.mock
    async def test_token_expired_check_leaves_a_token_with_no_exp_to_django_unchanged(self, connect_server):
        token = _jwt()
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json={"results": []}))
        resp = await _call(connect_server, token, "list_projects", {})
        assert resp.status_code == 200 and upstream.calls[0].request.headers["authorization"] == token

    @respx.mock
    async def test_token_expired_cap_also_bounds_the_terrain_import_poll(self, connect_server, fake_clock):
        respx.get(f"{BASE}/projects/7/terrain/").mock(
            return_value=httpx.Response(200, json={"count": 1, "results": [{"id": 3, "status": "importing"}]})
        )
        result = await _result(
            connect_server, _jwt(exp=int(time.time()) + 50), "get_terrain", {"project_id": 7, "timeout_seconds": 180}
        )
        assert result.get("isError") is not True, result["content"][0]["text"]
        assert 28.0 <= fake_clock.t <= 30.0

    @respx.mock
    async def test_token_expired_cap_skips_an_agent_key_with_no_exp(self, connect_server, fake_clock):
        respx.get(f"{BASE}/runs/9/status/").mock(return_value=_computing())
        data = _tool_json(
            await _result(connect_server, CALLER_KEY, "get_run_status", {"run_id": 9, "wait_seconds": 180})
        )
        assert data["waited_seconds"] == 180.0 and fake_clock.t == 180.0

    @pytest.mark.parametrize("exp", ["soon", None, True, [1]])
    @respx.mock
    async def test_token_expired_cap_skips_a_token_with_no_readable_exp(self, connect_server, fake_clock, exp):
        respx.get(f"{BASE}/runs/9/status/").mock(return_value=_computing())
        auth = _jwt() if exp is None else _jwt(exp=exp)
        data = _tool_json(await _result(connect_server, auth, "get_run_status", {"run_id": 9, "wait_seconds": 60}))
        assert data["waited_seconds"] == 60.0

    async def test_token_expired_cap_helper_reads_exp_unverified(self, connect_server):
        from hydrata_mcp.client import bearer_exp

        assert bearer_exp(_jwt(exp=1234)) == 1234
        for value in (None, "", CALLER_KEY, "Bearer a.b.c", "Bearer not-a-jwt", _jwt(exp="x"), _jwt(exp=float("nan"))):
            assert bearer_exp(value) is None
        payload = base64.urlsafe_b64encode(b'{"exp": 1' + b"0" * 400 + b"}").decode().rstrip("=")
        assert bearer_exp(f"Bearer {_b64({'alg': 'RS256'})}.{payload}.c2ln") is None


# ---------------------------------------------------------------------------
# R3: Django's AGENT_TOKEN_EXPIRED is relayed as resumable, never as a revocation
# ---------------------------------------------------------------------------
class TestDjangoTokenExpired:
    @respx.mock
    async def test_token_expired_from_django_is_a_resumable_result_not_a_revocation(self, connect_server):
        respx.get(f"{BASE}/runs/9/status/").mock(return_value=httpx.Response(401, json=EXPIRED_BODY))
        result = await _result(connect_server, _jwt(exp=int(time.time()) + 3600), "get_run_status", {"run_id": 9})
        data = _tool_json(result)
        assert data["error_code"] == "AGENT_TOKEN_EXPIRED" and data["retryable"] is True
        assert data["http_status"] == 401 and data["outcome"] == "token_expired"
        assert "Call the same tool again" in data["next_step"]
        assert "Do NOT ask the person to reconnect" in data["next_step"]
        text = result["content"][0]["text"]
        assert "revoked on the Agent keys page" not in text and "Connect again" not in text

    @respx.mock
    async def test_token_expired_mid_wait_ends_resumable_not_agent_key_invalid(self, connect_server, fake_clock):
        """AC3: the token ages out between two polls of a wait (no readable exp to cap by): resumable, not a refusal."""
        respx.get(f"{BASE}/runs/9/status/").mock(
            side_effect=[_computing(), _computing(), httpx.Response(401, json=EXPIRED_BODY)]
        )
        data = _tool_json(await _result(connect_server, _jwt(), "get_run_status", {"run_id": 9, "wait_seconds": 180}))
        assert data["error_code"] == "AGENT_TOKEN_EXPIRED" and data["retryable"] is True

    @respx.mock
    async def test_token_expired_on_a_tool_that_reads_4xx_bodies_is_still_resumable(self, connect_server):
        """get_observation_comparison hands 4xx back as data (raise_for_status=False): an expiry is not data."""
        respx.get(url__startswith=f"{BASE}/runs/9/").mock(return_value=httpx.Response(401, json=EXPIRED_BODY))
        data = _tool_json(
            await _result(
                connect_server, _jwt(exp=int(time.time()) + 3600), "get_observation_comparison", {"run_id": 9}
            )
        )
        assert data["error_code"] == "AGENT_TOKEN_EXPIRED" and data["retryable"] is True

    @respx.mock
    async def test_token_expired_regression_a_revoked_grant_still_relays_agent_key_invalid(self, connect_server):
        respx.get(f"{BASE}/runs/9/status/").mock(
            return_value=httpx.Response(401, json={"detail": REVOKED_DETAIL, "error_code": "AGENT_KEY_INVALID"})
        )
        text = _tool_error_text(
            await _result(connect_server, _jwt(exp=int(time.time()) + 3600), "get_run_status", {"run_id": 9})
        )
        assert "AGENT_KEY_INVALID" in text and REVOKED_DETAIL in text
        assert "AGENT_TOKEN_EXPIRED" not in text and "retryable" not in text

    async def test_token_expired_only_agent_key_invalid_carries_the_revoked_words(self, connect_server):
        """The MCP server writes no 'revoked ... connect again' text of its own: only Django's body carries it."""
        assert "revoked on the Agent keys page" not in connect_server.module_source()
