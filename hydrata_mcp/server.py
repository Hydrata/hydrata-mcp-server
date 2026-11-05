"""Hydrata MCP Server — 29 hand-crafted tools for ANUGA flood simulation."""

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from starlette.middleware import Middleware
from starlette.responses import Response

from ._common import config, mcp
from ._helpers import _ui_origin
from .client import bearer_exp

# Importing a tool-family module registers its tools on `mcp` (their `@_tool`
# decorators run at import), so THIS ORDER IS THE tools/list ORDER: keep it
# explicit and do not let the import sorter reorder it. `scenarios` comes last
# because get_scenario reads `inputs._unbound_series`.
# isort: off
from . import projects  # noqa: F401
from . import runs  # noqa: F401
from . import terrain  # noqa: F401
from . import inputs  # noqa: F401
from . import observations  # noqa: F401
from . import calibration  # noqa: F401
from . import scenarios  # noqa: F401
from . import feedback  # noqa: F401
# isort: on


# ---------------------------------------------------------------------------
# TASK-3166 (W0.1, epic 2467) — credential pass-through
# ---------------------------------------------------------------------------
# TASK-3508 (W2a, epic 3200, D25) — the credential is an hk_ agent key: a named,
# revocable grant the user creates on <site>/agents/, sent as
# ``Authorization: Bearer hk_…`` and VERIFIED BY DJANGO (this server never
# validates it; it forwards it verbatim).
# TASK-3730 (W2a gate fix, D25 amended 15-10-26) — and ONLY an agent key: a
# tools/call carrying HTTP Basic or any other Bearer token (a GeoNode OAuth
# token) is 401 here, with no grace period. Such a caller would act as the
# user while skipping the key level, the kill switch, the spend cap/consent
# and the agent activity log. Django keeps Basic for QGIS/scripts.
AGENT_KEY_HEADER = "Authorization: Bearer hk_..."
AGENT_KEY_PREFIX = "hk_"


def _agent_keys_url() -> str:
    return f"{_ui_origin()}/agents/"


def _is_agent_key(value: str) -> bool:
    """True when Django's ``bearer_agent_key`` (apps/agents/keys.py) would read
    ``value`` as an agent key: scheme ``Bearer`` in any case, then the token
    after extra spaces, starting ``hk_``. Same parse, so the two never disagree
    on what is a key. The prefix only; Django verifies the key itself."""
    scheme, _, token = value.strip().partition(" ")
    return scheme.lower() == "bearer" and token.strip().startswith(AGENT_KEY_PREFIX)


_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


def _is_connect_token(value: str) -> bool:
    """TASK-3512: True for ``Bearer <JWT>`` when the one-click connect flow is configured here.

    Shape only, and only with ``HYDRATA_MCP_RESOURCE_URL`` set. This server never
    validates the token (it holds no key and no identity): it is forwarded verbatim
    and Django checks signature, issuer, audience and the grant on every call.
    A GeoNode OAuth token is opaque (no dots), so it is still refused.
    """
    if not config.resource_url:
        return False
    scheme, _, token = value.strip().partition(" ")
    token = token.strip()
    return scheme.lower() == "bearer" and not token.startswith(AGENT_KEY_PREFIX) and bool(_JWT_SHAPE.match(token))


def _resource_metadata_url() -> str:
    """Where the protected-resource metadata for ``config.resource_url`` is served (RFC 9728 §3.1)."""
    parts = urlsplit(config.resource_url)
    return f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource{parts.path}"


def _connect_token_expired(value: str) -> bool:
    """True when a connect-flow token's ``exp`` has passed (read unverified; RFC 6750 §3.1).

    An OAuth client refreshes on a 401 ``invalid_token``; forwarded to Django, an
    expired token only came back as a tool error inside a 200, so the client never
    refreshed. A payload without a readable numeric ``exp`` is left to Django.
    """
    exp = bearer_exp(value)  # TASK-4204: the one unverified exp reader (it also caps every wait)
    return exp is not None and exp <= time.time()


# TASK-4177 — what an allowlisted catalog crawler may do with no credential: the
# handshake and the catalog. Never tools/call (or resources/*, prompts/*, …):
# anything that reaches Hydrata, or might tomorrow, needs a caller.
DISCOVERY_METHODS = frozenset({"initialize", "notifications/initialized", "ping", "tools/list"})


def _is_discovery_crawler(scope) -> bool:
    """True when a User-Agent header contains an allowlisted crawler substring (any case).

    ``config.discovery_user_agents`` is already lower-cased; empty disables this.
    """
    if not config.discovery_user_agents:
        return False
    agents = [value.decode("latin-1").lower() for name, value in scope.get("headers", []) if name == b"user-agent"]
    return any(token in agent for agent in agents for token in config.discovery_user_agents)


def _www_authenticate(error: str = "") -> bytes:
    challenge = 'Bearer realm="hydrata.com"'
    if error:
        challenge += f', error="{error}"'
    if config.resource_url:
        challenge += f', resource_metadata="{_resource_metadata_url()}"'
    return challenge.encode()


class RequireAuth:
    """Pure-ASGI middleware: 401 every anonymous JSON-RPC request, before fastmcp runs.

    The server holds no identity of its own. Every request must carry the caller's
    ``Authorization`` header — an agent key, ``Bearer hk_…`` (TASK-3508) — which
    :class:`HydrataClient` forwards verbatim to the REST API, where Django verifies
    it, so results are scoped by the real user's project permissions and the grant's
    level, and the audit trail names them. Without exactly one such header — none,
    HTTP Basic, a non-``hk_`` Bearer token, or two Authorization headers
    (TASK-3730) — the request is answered here with ``401`` +
    ``WWW-Authenticate: Bearer realm="hydrata.com"`` and a JSON-RPC-shaped error
    body (code ``-32001``) that an MCP client can surface, before fastmcp ever
    runs a tool.

    Why refuse two headers: the client forwards the LAST Authorization
    (fastmcp's ``get_http_headers`` builds a dict), so checking any one of
    several would let ``Bearer hk_…`` + ``Basic …`` pass here and go upstream
    as Basic.

    Why EVERY method, not only ``tools/call`` (TASK-3987, superseding decision D2
    on TASK-2467): an anonymous ``initialize`` that answered 200 told claude.ai's
    Add custom connector dialog the server was authless, so it pre-selected "No
    sign-in" and the first connect failed. A 401 + ``WWW-Authenticate`` on the
    first request is what an auth-required server answers (MCP authorization
    spec; claude.com connector authentication docs). The anonymous tool catalog
    and MCP-directory liveness probes were given up for it, until TASK-4177 below.

    TASK-4177 — the catalog-crawler allowlist, the ONE exception. A request with NO
    ``Authorization`` header whose ``User-Agent`` contains one of
    ``config.discovery_user_agents`` (``HYDRATA_MCP_DISCOVERY_USER_AGENTS``; default
    ``mcptry.com indexer,SmitheryBot``; case-insensitive substring; set it empty to
    turn this off) is let through for :data:`DISCOVERY_METHODS` only
    (``initialize``, ``notifications/initialized``, ``ping``, ``tools/list``), so
    MCP catalogs that probe without credentials can list the tools. Everything else
    is unchanged: ``tools/call`` (or any other method, or a batch holding one) still
    401s with the same challenge, and a request that carries an ``Authorization``
    header follows the rules above whatever its User-Agent (a bad token still 401s).
    Why matching a spoofable header is acceptable: the catalog is public anyway (the
    README lists every tool), so a client that fakes the User-Agent learns only that
    list; ``tools/call`` always needs a credential that Django verifies. And why it
    does not bring TASK-3987 back: no real client (claude.ai, ChatGPT, Claude Code,
    Cursor, VS Code) sends a crawler User-Agent, so every one of them still gets the
    401 on its first ``initialize`` and keeps defaulting to sign-in.

    Why this shape and not the built-ins: ``fastmcp.server.auth`` is Bearer-only
    (it would refuse the ``hk_`` passthrough rules above); Starlette's
    ``BaseHTTPMiddleware`` needs explicit body replay. This reads the body once,
    decides, and replays it to the app untouched. A body that is not JSON (or not
    a dict/list, or nested too deep to decode) is passed through as-is so fastmcp
    emits its own ``-32700`` parse error — never a 500 from here. Batches (arrays)
    are inspected element-wise as defence in depth; the MCP SDK itself rejects
    them downstream. Only POST is gated: a GET (the SSE stream) goes to fastmcp,
    which answers 405 in stateless mode and so serves nothing to anyone.

    TASK-3512 (W2e): with ``HYDRATA_MCP_RESOURCE_URL`` set, the 401 also carries
    ``resource_metadata="…"`` so an OAuth-aware client (claude.ai, ChatGPT) can
    find the authorization server, and a ``Bearer <JWT>`` from that flow is let
    through and forwarded verbatim — Django verifies it (this server never does).
    With it unset, the agent key is the only credential and the PRM stays a 404.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        # Drain the request body (may arrive in chunks); stop on a disconnect.
        chunks: list[bytes] = []
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        try:
            parsed = json.loads(body)
        except (ValueError, RecursionError):
            # RecursionError: a pathologically nested body (~100 KB of '[')
            # overflows the decoder and is NOT a ValueError; it must be treated
            # like any other unparseable body, or it escapes as a 500 (W0 sweep).
            parsed = None
        messages = [m for m in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(m, dict)]
        # Any JSON-RPC message needs a caller (TASK-3987). The error echoes the id
        # of the batch's tools/call if there is one, else the first message's.
        needs_auth = bool(messages)
        calls = [m for m in messages if m.get("method") == "tools/call"]
        request_id = (calls or messages)[0].get("id") if messages else None

        # ASGI header names are lowercase bytes; a blank value is not a credential.
        auths = [value.decode("latin-1") for name, value in scope.get("headers", []) if name == b"authorization"]
        credentials = [a for a in auths if a.strip()]
        # TASK-4177: an allowlisted catalog crawler with NO Authorization header at
        # all may run the discovery methods. Every message must be one: a batch that
        # smuggles in a tools/call is refused whole.
        if (
            needs_auth
            and not auths
            and all(m.get("method") in DISCOVERY_METHODS for m in messages)
            and _is_discovery_crawler(scope)
        ):
            needs_auth = False
        refusal = None
        token_error = ""
        if needs_auth and not credentials:
            refusal = (
                f"Authentication required: send a Hydrata agent key as the header "
                f"'{AGENT_KEY_HEADER}' (create one at {_agent_keys_url()})"
            )
        elif needs_auth and (len(auths) != 1 or not (_is_agent_key(auths[0]) or _is_connect_token(auths[0]))):
            # TASK-3730: never echo the credential back — only what to send instead.
            refusal = (
                f"Only a Hydrata agent key is accepted: send exactly one header "
                f"'{AGENT_KEY_HEADER}'. Sign-in with HTTP Basic or any other token is not "
                f"accepted here. Create an agent key at {_agent_keys_url()}"
            )
        elif needs_auth and _is_connect_token(auths[0]) and _connect_token_expired(auths[0]):
            token_error = "invalid_token"
            refusal = "The access token has expired: refresh it, or connect again from your AI assistant."

        if refusal:
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32001, "message": refusal},
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode()),
                        (b"www-authenticate", _www_authenticate(token_error)),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload, "more_body": False})
            return

        # Replay the drained body once, then hand the real receive back so a
        # later disconnect still reaches the app.
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                if disconnected:
                    return {"type": "http.disconnect"}
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


# ---------------------------------------------------------------------------
# TASK-4177 — the static MCP Server Card (SEP-2127), GET <mcp-url>/server-card
# ---------------------------------------------------------------------------
# Served at the path SEP-2127 reserves, relative to the streamable-HTTP URL: the
# app is mounted at "/" behind nginx's `location /mcp/` (proxy_pass .../), so
# https://hydrata.com/mcp/server-card reaches this route as /server-card. Built
# from the repo's server.json (the MCP Registry entry), so the two cannot drift;
# the version in it is kept equal to pyproject.toml by `make parity`. A card
# deliberately carries no tools: those vary per caller and are read live with
# tools/list. Public, read-only metadata: no credential needed (RequireAuth gates
# only POST), CORS open and cacheable as the SEP's discovery doc asks.
SERVER_JSON = Path(__file__).resolve().parent.parent / "server.json"
SERVER_CARD_SCHEMA = "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
SERVER_CARD_MEDIA_TYPE = "application/mcp-server-card+json"
# The ServerCard properties server.json also has; packages and the registry's own
# $schema are not part of a card.
SERVER_CARD_FIELDS = ("name", "title", "description", "version", "websiteUrl", "repository", "icons", "remotes")
SERVER_CARD_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET",
    "Access-Control-Allow-Headers": "Content-Type, If-None-Match",
    "Access-Control-Expose-Headers": "ETag",
}


def build_server_card(server_json: Path = SERVER_JSON) -> dict:
    """The Server Card: ``$schema`` plus server.json's card fields, unchanged."""
    registry = json.loads(server_json.read_text())
    return {"$schema": SERVER_CARD_SCHEMA, **{key: registry[key] for key in SERVER_CARD_FIELDS if key in registry}}


try:
    # Prod runs an editable install from the clone (/opt/hydrata-mcp), so server.json
    # sits next to the package. A wheel without it serves a 404, never a crash.
    SERVER_CARD_BODY: bytes | None = json.dumps(build_server_card(), indent=2).encode()
except FileNotFoundError:
    SERVER_CARD_BODY = None
SERVER_CARD_ETAG = f'"{hashlib.sha256(SERVER_CARD_BODY).hexdigest()[:32]}"' if SERVER_CARD_BODY else ""


@mcp.custom_route("/server-card", methods=["GET", "OPTIONS"])
async def server_card(request):
    """TASK-4177: the Server Card, with CORS, ``Cache-Control``, ``ETag`` and ``304`` on ``If-None-Match``."""
    if SERVER_CARD_BODY is None:
        return Response(status_code=404, headers=SERVER_CARD_CORS)
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=SERVER_CARD_CORS)
    headers = {**SERVER_CARD_CORS, "Cache-Control": "public, max-age=3600", "ETag": SERVER_CARD_ETAG, "Vary": "Accept"}
    validators = [tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match", "").split(",")]
    if "*" in validators or SERVER_CARD_ETAG in validators:
        return Response(status_code=304, headers=headers)
    # Echo the card media type to a client that asks for it; plain JSON otherwise.
    media_type = (
        SERVER_CARD_MEDIA_TYPE if SERVER_CARD_MEDIA_TYPE in request.headers.get("accept", "") else "application/json"
    )
    return Response(SERVER_CARD_BODY, media_type=media_type, headers=headers)


# TASK-4177: the catalog icon (server.json `icons`, serverInfo.icons), a 512x512
# square PNG of the Hydrata mark. Served here, not from the website's static
# files, so it shares the server's origin (the MCP spec asks clients to check an
# icon's origin) and ships with the version that lists it. Public, like the card.
ICON_FILE = Path(__file__).resolve().parent / "assets" / "icon.png"
try:
    ICON_BODY: bytes | None = ICON_FILE.read_bytes()
except FileNotFoundError:
    ICON_BODY = None
ICON_ETAG = f'"{hashlib.sha256(ICON_BODY).hexdigest()[:32]}"' if ICON_BODY else ""
ICON_HEADERS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"}


@mcp.custom_route("/icon.png", methods=["GET"])
async def icon(request):
    """TASK-4177: the catalog icon, with CORS, ``Cache-Control``, ``ETag`` and ``304`` on ``If-None-Match``."""
    if ICON_BODY is None:
        return Response(status_code=404)
    headers = {**ICON_HEADERS, "ETag": ICON_ETAG}
    validators = [tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match", "").split(",")]
    if "*" in validators or ICON_ETAG in validators:
        return Response(status_code=304, headers=headers)
    return Response(ICON_BODY, media_type="image/png", headers=headers)


# One list, baked into the one `app` below, which BOTH entry points serve: prod's
# unit file (`uvicorn hydrata_mcp.server:app`) and the `hydrata-mcp` CLI. An
# unguarded CLI entry would be a second, anonymous server.
MIDDLEWARE = [Middleware(RequireAuth)]


# ---------------------------------------------------------------------------
# ASGI app factory + CLI entry point
# ---------------------------------------------------------------------------
def create_app():
    """Create ASGI application for uvicorn."""
    return mcp.http_app(path="/", stateless_http=True, middleware=MIDDLEWARE)


# Module-level ASGI app for `uvicorn hydrata_mcp.server:app`
app = create_app()


def main():
    """CLI entry point: `hydrata-mcp`.

    Runs uvicorn on the identical ``app`` that prod's unit file runs
    (``uvicorn hydrata_mcp.server:app``): one server object, one path (``/``),
    one middleware list. TASK-3181 (W1.0, epic 2467) — fastmcp's own runner
    built a SECOND app at its default path, so the CLI and prod disagreed on
    where the endpoint was (pre-existing since 7586f72, which moved ``create_app``
    to ``/`` only).
    """
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
