"""TASK-3512 (W2e, epic 3200) — the one-click connect Bearer JWT on /mcp/.

With HYDRATA_MCP_RESOURCE_URL set, a 401 names the protected-resource metadata
and a ``Bearer <JWT>`` tools/call is forwarded to Django verbatim (Django
verifies it; this server never does). Unset, nothing changes: agent keys only.
"""
import importlib

import httpx
import pytest
import respx

from tests.test_server import (BASE, CALLER_BASIC, CALLER_KEY, CALLER_OAUTH, UPSTREAM_OK, _call,
                               _server)  # noqa: F401  (_server is the shared fixture)

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


class TestConnectOff:
    @respx.mock
    async def test_a_jwt_is_refused_exactly_as_before(self, _server):
        upstream = respx.route().mock(return_value=httpx.Response(200, json=UPSTREAM_OK))
        resp = await _call(_server, JWT)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Bearer realm="hydrata.com"'
        assert not upstream.calls
