"""TASK-3512 (W2e, epic 3200) — the one-click connect Bearer JWT on /mcp/.

With HYDRATA_MCP_RESOURCE_URL set, a 401 names the protected-resource metadata
and a ``Bearer <JWT>`` tools/call is forwarded to Django verbatim (Django
verifies it; this server never does). Unset, nothing changes: agent keys only.
"""
import base64
import importlib
import json
import time

import httpx
import pytest
import respx

from tests.test_server import (
    BASE,
    CALLER_BASIC,
    CALLER_KEY,
    CALLER_OAUTH,
    EVERY_METHOD,
    INITIALIZE_PARAMS,
    MCP_HEADERS,
    UPSTREAM_OK,
    _asgi_client,
    _call,
    _parse,
    _rpc,
    _server,  # noqa: F401  (_server is the shared fixture)
)

pytestmark = pytest.mark.asyncio

RESOURCE = "https://hydrata.com/mcp/"
METADATA = "https://hydrata.com/.well-known/oauth-protected-resource/mcp/"
# header.payload.signature — base64url segments, not a real token
JWT = "Bearer eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.eyJzdWIiOiJ1c2VyXzAxIn0.c2lnbmF0dXJl"


@pytest.fixture
def connect_server(env_vars, monkeypatch):
    monkeypatch.setenv("HYDRATA_MCP_RESOURCE_URL", RESOURCE)
    import hydrata_mcp.server as srv
    importlib.reload(srv)
    return srv


class TestConnectOn:
    async def test_anonymous_401_carries_the_resource_metadata(self, connect_server):
        resp = await _call(connect_server, " ")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == f'Bearer realm="hydrata.com", resource_metadata="{METADATA}"'

    @respx.mock
    async def test_a_jwt_bearer_is_forwarded_verbatim(self, connect_server):
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(connect_server, JWT)
        assert resp.status_code == 200
        assert upstream.calls[0].request.headers["authorization"] == JWT

    @respx.mock
    async def test_a_key_still_passes(self, connect_server):
        respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        assert (await _call(connect_server, CALLER_KEY)).status_code == 200

    @pytest.mark.parametrize("auth", [CALLER_BASIC, CALLER_OAUTH, "Bearer a.b", "Bearer a.b.c.d",
                                      "Basic YS5iLmM=", "Token a.b.c"])
    @respx.mock
    async def test_everything_else_is_still_401(self, connect_server, auth):
        upstream = respx.route().mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(connect_server, auth)
        assert resp.status_code == 401
        assert not upstream.calls


async def _rpc_as(srv, auth, method, params):
    headers = dict(MCP_HEADERS) if auth is None else {**MCP_HEADERS, "Authorization": auth}
    async with _asgi_client(srv) as c:
        return await c.post("/", headers=headers, json=_rpc(method, params))


class TestEveryMethodNeedsSignIn:
    """TASK-3987: claude.ai's Add custom connector dialog pre-selected "No sign-in"
    because an anonymous initialize answered 200. Every method now answers an
    anonymous caller with 401 + the resource_metadata pointer, as the MCP
    authorization spec expects of a server that requires auth."""

    @pytest.mark.parametrize("auth", [None, " "], ids=["no-header", "blank-header"])
    @pytest.mark.parametrize("method,params", EVERY_METHOD)
    async def test_anonymous_is_401_naming_the_metadata(self, connect_server, method, params, auth):
        resp = await _rpc_as(connect_server, auth, method, params)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == f'Bearer realm="hydrata.com", resource_metadata="{METADATA}"'
        assert resp.json()["error"]["code"] == -32001

    @pytest.mark.parametrize("auth", [JWT, CALLER_KEY], ids=["connect-jwt", "agent-key"])
    @pytest.mark.parametrize("method,params", EVERY_METHOD)
    async def test_signed_in_is_200(self, connect_server, method, params, auth):
        resp = await _rpc_as(connect_server, auth, method, params)
        assert resp.status_code == 200
        assert "error" not in _parse(resp)

    async def test_expired_jwt_initialize_is_401_invalid_token(self, connect_server):
        """The client refreshes on this 401, whichever method it was sending."""
        resp = await _rpc_as(connect_server, _jwt({"sub": "user_01", "exp": int(time.time()) - 10}),
                             "initialize", INITIALIZE_PARAMS)
        assert resp.status_code == 401
        assert 'error="invalid_token"' in resp.headers["www-authenticate"]


class TestConnectOff:
    @respx.mock
    async def test_a_jwt_is_refused_exactly_as_before(self, _server):  # noqa: F811
        upstream = respx.route().mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(_server, JWT)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Bearer realm="hydrata.com"'
        assert not upstream.calls


def _jwt(payload: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"Bearer {seg({'alg': 'RS256', 'kid': 'k1'})}.{seg(payload)}.c2lnbmF0dXJl"


class TestExpiredConnectToken:
    """An expired connect-flow token is an HTTP 401, not a 200 with a tool error.

    The MCP authorization spec says invalid or expired tokens get HTTP 401; an OAuth
    client (claude.ai) refreshes on that 401. Forwarded to Django, the expiry only
    came back as ``isError`` inside a 200 stream, so the client never refreshed and
    the person was told to reconnect about an hour after connecting (W2e live setup,
    18-10-26). Only ``exp`` is read here, unverified; Django still checks everything.
    """

    @respx.mock
    async def test_expired_is_401_naming_invalid_token(self, connect_server):
        upstream = respx.route().mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(connect_server, _jwt({"sub": "user_01", "exp": int(time.time()) - 10}))
        assert resp.status_code == 401
        challenge = resp.headers["www-authenticate"]
        assert 'error="invalid_token"' in challenge
        assert f'resource_metadata="{METADATA}"' in challenge
        assert not upstream.calls

    @respx.mock
    async def test_unexpired_is_forwarded(self, connect_server):
        token = _jwt({"sub": "user_01", "exp": int(time.time()) + 600})
        upstream = respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(connect_server, token)
        assert resp.status_code == 200
        assert upstream.calls[0].request.headers["authorization"] == token

    @pytest.mark.parametrize("token", [JWT, "Bearer eyJhIjoxfQ.bm90IGpzb24.c2ln",
                                       _jwt({"sub": "user_01", "exp": "soon"})])
    @respx.mock
    async def test_no_readable_exp_is_left_to_django(self, connect_server, token):
        respx.get(f"{BASE}/projects/").mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        assert (await _call(connect_server, token)).status_code == 200
