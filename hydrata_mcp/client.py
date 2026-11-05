"""Async HTTP client wrapper for Hydrata REST API."""

import math
import time
import uuid
from contextvars import ContextVar
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastmcp.server.dependencies import get_context, get_http_headers

from . import telemetry
from .config import Config

try:
    PACKAGE_VERSION = version("hydrata-mcp-server")
except PackageNotFoundError:  # a source checkout run without an install
    PACKAGE_VERSION = "0.0.0"
# TASK-3203 (W1, epic 3200) — who is calling, sent on every forwarded request.
# INFORMATIONAL only: both headers are client-asserted on the public 443 hop,
# so Django must never stamp authorship from them (agent authorship comes from
# the nginx loopback listener, W2.1).
CLIENT_HEADER = "X-Hydrata-Client"
SESSION_HEADER = "X-Hydrata-Session"
CLIENT_HEADER_VALUE = f"mcp/{PACKAGE_VERSION}"


def session_header_value() -> str:
    """A UUID naming the MCP session the current tool call belongs to.

    The server runs ``stateless_http=True``: it issues no ``Mcp-Session-Id``,
    so a session is only identifiable when the CLIENT sends one (a stateful
    client or proxy). That header is reused when present — normalised to a
    UUID (uuid5 of a non-UUID token) so the value is always UUID-shaped.
    Otherwise fastmcp's per-request session id (one UUID for the whole tool
    call, so every upstream request of one call shares it); outside any
    request (direct invocation, tests) a fresh UUID.
    """
    inbound = get_http_headers(include={"mcp-session-id"}).get("mcp-session-id", "").strip()
    if inbound:
        try:
            return str(uuid.UUID(inbound))
        except ValueError:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mcp-session:{inbound}"))
    try:
        candidate = get_context().session_id
    except RuntimeError:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(candidate)))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mcp-session:{candidate}"))


# ---------------------------------------------------------------------------
# TASK-4204 (epic 4117 W7h) — a sign-in token that ages out during a long call.
#
# A tool call keeps the bearer it started with. A wait of minutes (get_run_status wait_seconds, a terrain
# import poll) can outlive a WorkOS access token, and Django then refuses the next poll. So every wait stops
# TOKEN_EXPIRY_MARGIN_SECONDS before the token's ``exp`` and returns the tool's normal resumable "still
# waiting, call again" result; the next call carries the client's refreshed token. The ``exp`` is read
# UNVERIFIED and used for timing ONLY, never for an auth decision: Django stays the only verifier. A
# credential with no readable ``exp`` (an hk_ agent key, a non-JWT) is not capped.
# If Django still answers 401 AGENT_TOKEN_EXPIRED, the registered tool returns token_expired_result():
# resumable, "call the same tool again", never "reconnect" (that text is Django's AGENT_KEY_INVALID only).
# ---------------------------------------------------------------------------
TOKEN_EXPIRED = "AGENT_TOKEN_EXPIRED"
TOKEN_EXPIRY_MARGIN_SECONDS = 20
_UNREAD = object()
_call_token_exp: ContextVar = ContextVar("hydrata_call_token_exp", default=_UNREAD)


def bearer_exp(authorization: str | None) -> float | None:
    """The ``exp`` of a ``Bearer <JWT>`` header, read without verification; None when there is none to read."""
    token = (authorization or "").strip().partition(" ")[2].strip()
    if token.count(".") != 2:
        return None
    exp = telemetry._jwt_claims(token).get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    try:
        return exp if math.isfinite(exp) else None
    except OverflowError:  # a huge int (1e400 digits) cannot become a float: not a usable exp
        return None


def _caller_authorization() -> str | None:
    return get_http_headers(include={"authorization"}).get("authorization")


def note_call_token():
    """At the start of a tool call: remember its bearer's ``exp``. Returns the ContextVar token to reset with."""
    return _call_token_exp.set(bearer_exp(_caller_authorization()))


def forget_call_token(token) -> None:
    _call_token_exp.reset(token)


def token_wait_budget() -> float | None:
    """Seconds a wait may still run before this call's token is too close to its ``exp``; None = no cap."""
    exp = _call_token_exp.get()
    if exp is _UNREAD:  # a direct call outside a registered tool (tests): read the header now
        exp = bearer_exp(_caller_authorization())
    return None if exp is None else exp - time.time() - TOKEN_EXPIRY_MARGIN_SECONDS


TOKEN_EXPIRED_NEXT_STEP = (
    "Your sign-in token expired during this call. Nothing was revoked and the connection is still live. "
    "Do NOT ask the person to reconnect. Call the same tool again with the same arguments: your client sends "
    "a fresh token. If this tool writes, read first (read_input_layer, list_inputs, get_scenario, get_run_status) "
    "whether part of it already landed, so nothing is done twice."
)


def token_expired_result(exc: "HydrataAPIError") -> dict:
    """The resumable result a tool returns when Django answered 401 AGENT_TOKEN_EXPIRED."""
    return {
        "outcome": "token_expired",
        "error_code": TOKEN_EXPIRED,
        "retryable": True,
        "http_status": 401,
        "detail": str(exc),
        "next_step": TOKEN_EXPIRED_NEXT_STEP,
    }


class HydrataAPIError(Exception):
    """Raised when the Hydrata API is unreachable or returns an error.

    FastMCP catches this and returns ``isError: true`` with the message,
    giving agents a clean signal instead of a raw traceback.

    TASK-3961: ``status`` is the HTTP status Django answered (None when there was no
    answer: connect error, timeout, or a refusal the tool raised before calling out) and
    ``code`` the body's ``error_code``. The call record classifies on these, never on the
    class, because this exception is raised for EVERY 4xx and 5xx.
    """

    def __init__(self, message: str = "", status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _client_error_message(exc: httpx.HTTPStatusError, method: str, path: str) -> str:
    """Status + reason phrase + the API's own body (``error_code`` / ``detail``).

    TASK-2469 (W1.1, epic 2467) — the reason phrase alone ("400: Bad Request")
    hid the one thing the agent needs to act on: a finalize against a key whose
    PUT never landed answers ``UPLOAD_NOT_FOUND``, a presign over 5 GiB answers
    a VALIDATION_ERROR detail, and W1.3's build refusal is a 422 whose body IS
    the message. The body is clipped so a stray HTML error page cannot flood
    the tool result.
    """
    resp = exc.response
    message = f"Hydrata API returned {resp.status_code}: {resp.reason_phrase} for {method} {path}"
    detail = resp.text.strip()[:1000]
    if detail:
        message = f"{message} — {detail}"
    return message


def _error_code(resp: httpx.Response) -> str | None:
    """The body's ``error_code`` when the reply is a JSON object carrying a string one."""
    try:
        body = resp.json()
    except ValueError:
        return None
    code = body.get("error_code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _is_token_expired(resp: httpx.Response) -> bool:
    """TASK-4204: Django's 401 AGENT_TOKEN_EXPIRED. Never data a tool reads: always raised, whatever the flag."""
    return resp.status_code == 401 and _error_code(resp) == TOKEN_EXPIRED


def _is_agent_paused(resp: httpx.Response) -> bool:
    """Hydrata's agent kill switch (TASK-3510): ``503 AGENT_PAUSED`` + a human message.

    A deliberate refusal, not an outage: surface its body (via
    _client_error_message) so the agent reads "do not retry, tell the person"
    instead of "the backend may be experiencing issues".
    """
    if resp.status_code != 503:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error_code") == "AGENT_PAUSED"


class HydrataClient:
    """Thin async wrapper around the Hydrata /api/v2/anuga/ endpoints.

    Holds NO credential of its own. Each request forwards the calling MCP
    client's ``Authorization`` header verbatim (TASK-3166, W0.1, epic 2467),
    so GeoNode scopes results to the real user and the audit trail names them.
    Returns parsed JSON dicts.
    """

    def __init__(self, config: Config) -> None:
        self._base = config.api_url
        # TASK-3171 (W1.2, epic 2467) — GeoNode's own endpoints (the upload
        # execution-status, executionrequest and datasets routes) live at
        # /api/v2/…, NOT under HYDRATA_API_URL's /api/v2/anuga. The origin
        # (scheme + host[:port]) is derived from the ONE configured URL rather
        # than a second env var, so prod's 127.0.0.1:8081 internal listener and
        # localhost both resolve without new config, and the Host header W0.2
        # sets still applies.
        split = urlsplit(config.api_url)
        self._origin = f"{split.scheme}://{split.netloc}"
        self._api_host = config.api_host
        self._headers = {"Accept": "application/json"}
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    def _request_headers(self) -> dict[str, str]:
        """Per-request headers: the caller's Authorization, Host when configured,
        and the informational X-Hydrata-Client / X-Hydrata-Session (TASK-3203).

        TASK-3166 (W0.1, epic 2467) — ``get_http_headers()`` STRIPS ``authorization``
        (and ``cookie``) unless it is named in ``include``; the bare call would
        silently send an anonymous upstream request. Outside an HTTP request
        (direct tool invocation, tests) it returns ``{}`` and nothing is forwarded.
        Only Authorization is picked out — the rest of the inbound headers
        (user-agent, accept-language, …) are the MCP client's, not ours to relay.
        """
        headers: dict[str, str] = {
            CLIENT_HEADER: CLIENT_HEADER_VALUE,
            SESSION_HEADER: session_header_value(),
            # TASK-3961: joins the Django AgentActionLog row, the nginx line and the call record.
            telemetry.REQUEST_ID_HEADER: telemetry.current_request_id(),
        }
        auth = _caller_authorization()
        if auth:
            headers["Authorization"] = auth
        if self._api_host:
            headers["Host"] = self._api_host
        return headers

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _send(
        self, method: str, url: str, path: str, raise_for_status: bool = True, relay_statuses: tuple = (), **kwargs
    ) -> httpx.Response:
        """One request with the caller's headers; every failure becomes a HydrataAPIError.

        `path` is only for the message (the part after the base, as the tool
        wrote it). 5xx bodies are deliberately NOT relayed — they are HTML
        tracebacks, not a signal the agent can act on.

        TASK-3172 (W1.3, epic 2467) — ``raise_for_status=False`` hands a 4xx
        response BACK to the caller instead of raising: build_scenario must
        return the API's 422 MESH_TOO_LARGE / 409 bodies verbatim as a
        NON-error result the agent reads (estimate, ceiling, the in-flight
        run_id), not as an error string. It is not a second error path: a
        ConnectError, a timeout and every 5xx still map to HydrataAPIError
        exactly as before, only the 4xx-as-exception step is skipped. httpx
        does not follow redirects, so a 3xx would also come back under the
        flag — no tool path can hit one (every path ends in '/', so
        APPEND_SLASH never redirects).
        """
        client = await self._ensure_client()
        try:
            resp = await client.request(method, url, headers=self._request_headers(), **kwargs)
            telemetry.note_reply(resp)  # Django's X-Hydrata-Actor, on any status that carries it
            # Full review 03-11-26 (MCP-4): a 5xx named in ``relay_statuses`` is handed back with its body (the
            # 504 FLOW_HYDROGRAPH_TIMEOUT carries "try a shorter line"); every other 5xx is still an error.
            if (
                raise_for_status
                or (resp.is_server_error and resp.status_code not in relay_statuses)
                or _is_token_expired(resp)
            ):
                resp.raise_for_status()
        except httpx.ConnectError:
            raise HydrataAPIError(
                f"Hydrata API is unreachable at {self._base}. The backend may be restarting.",
                code="UPSTREAM_UNREACHABLE",
            )
        except httpx.TimeoutException:
            raise HydrataAPIError(
                "Hydrata API request timed out after 30s. Try again shortly.", code="UPSTREAM_TIMEOUT"
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            code = _error_code(exc.response)
            if status >= 500 and not _is_agent_paused(exc.response):
                raise HydrataAPIError(
                    f"Hydrata API server error ({status}). The backend may be experiencing issues.",
                    status=status,
                    code=code,
                )
            raise HydrataAPIError(_client_error_message(exc, method, path), status=status, code=code)
        return resp

    @staticmethod
    def _body(resp: httpx.Response) -> Any:
        """The JSON body; {} when empty; {"response": text} when it is not JSON.

        TASK-3172 (W1.3, epic 2467) — a 4xx handed back under
        ``raise_for_status=False`` may be an HTML page (nginx's 413, a proxy
        403) rather than DRF JSON; decoding it must not turn a legible
        refusal into a traceback. Clipped like _client_error_message.
        """
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"response": resp.text.strip()[:1000]}

    async def get(self, path: str, params: dict | None = None) -> Any:
        resp = await self._send("GET", f"{self._base}{path}", path, params=params)
        return resp.json()

    async def get_from_origin(self, path: str, params: dict | None = None) -> Any:
        """GET a path relative to the API's ORIGIN (e.g. ``/api/v2/datasets/1/``).

        TASK-3171 (W1.2, epic 2467) — for GeoNode endpoints outside /api/v2/anuga;
        `path` must start with '/'. Same headers, same error mapping as get().
        """
        resp = await self._send("GET", f"{self._origin}{path}", path, params=params)
        return resp.json()

    async def post(
        self,
        path: str,
        json: dict | None = None,
        raise_for_status: bool = True,
        relay_statuses: tuple = (),
        timeout: float | None = None,
    ) -> tuple[Any, int]:
        """POST request. Returns (body, status_code) since some endpoints return 202.

        With ``raise_for_status=False`` a 4xx is returned as ``(body, status)``
        too (TASK-3172: build_scenario surfaces 422/409 bodies verbatim);
        the default keeps every other tool's raise-on-4xx behaviour.
        """
        extra = {"timeout": httpx.Timeout(timeout, connect=10.0)} if timeout else {}
        resp = await self._send(
            "POST",
            f"{self._base}{path}",
            path,
            raise_for_status=raise_for_status,
            relay_statuses=relay_statuses,
            json=json or {},
            **extra,
        )
        return self._body(resp), resp.status_code

    async def post_from_origin(self, path: str, json: dict | None = None) -> tuple[Any, int]:
        """POST a path relative to the API's ORIGIN (e.g. ``/api/v2/agents/suggestions/``).

        TASK-3964 — for Hydrata endpoints outside /api/v2/anuga; `path` must start with '/'.
        Same headers and error mapping as post(): a 4xx raises HydrataAPIError, with its status.
        """
        resp = await self._send("POST", f"{self._origin}{path}", path, json=json or {})
        return self._body(resp), resp.status_code

    async def patch(self, path: str, json: dict | None = None) -> tuple[Any, int]:
        """PATCH request (partial update). Returns (body, status_code) like post().

        TASK-3171 (W1.2, epic 2467) — attach_input_layer sets ONE field
        (``gn_layer``) on one of the SIX default input rows. It is the only
        safe write: the four list+retrieve+update viewsets accept nothing
        else, and a POST to the two create-capable ones (buildings,
        mesh-regions) has its gn_layer overwritten by the async layer factory.
        """
        resp = await self._send("PATCH", f"{self._base}{path}", path, json=json or {})
        return self._body(resp), resp.status_code

    async def delete(self, path: str) -> int:
        """DELETE request; returns the status code (the API answers 204, no body).

        TASK-3812 (epic 3801) — delete_observation removes ONE feature of a flood
        observation layer (and its photos). Same headers and error mapping as
        every other verb: a 404 for a missing feature is a HydrataAPIError.
        """
        resp = await self._send("DELETE", f"{self._base}{path}", path)
        return resp.status_code

    async def get_with_status(self, path: str, params: dict | None = None) -> tuple[Any, int]:
        """GET that hands a 4xx BACK as ``(body, status)`` instead of raising.

        TASK-3816 (epic 3801) — get_observation_comparison must tell a run that
        has no comparison (404 ``NO_COMPARISON``: a design or unlabelled run, a
        normal answer) from a 404 that IS an error (a stranger's run). Same
        headers; a ConnectError, a timeout and every 5xx still raise.
        """
        resp = await self._send("GET", f"{self._base}{path}", path, raise_for_status=False, params=params)
        return self._body(resp), resp.status_code
