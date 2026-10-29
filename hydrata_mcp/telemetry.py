"""One structured record per tools/call (TASK-3961, epic 3958 D2/D3/D5), printed as a JSON line to stdout.

systemd journals stdout; promtail ships it (job=hydrata-mcp, kind label) to Loki, where the
"MCP Agents" dashboard and the MCP alerts read it. Three rules shape everything here:

* WHO is Django's answer, never ours. Django verifies the credential and tells us who it
  verified in the ``X-Hydrata-Actor`` reply header (``user_id=12&username=ann&grant=34&
  credential=agent_key&internal=0``). Only a call with no verified reply falls back to a
  fingerprint of the credential (``identity_source=credential``): an ``hk_`` key's id prefix
  or a hash of the JWT ``sub``. Nothing in the line is read from a client-asserted field
  except the display strings (client name, user agent), which are clipped.
* OUTCOME separates our faults from working-as-designed refusals, so alerts never page on a
  refusal. ``HydrataAPIError`` is raised for EVERY 4xx and 5xx; the class alone says nothing.
  ok / refused (a 4xx, the kill-switch 503 AGENT_PAUSED, a returned ``http_status`` >= 400) /
  invalid (unknown tool, bad arguments, a tool-side refusal with no status) / broken (any
  other 5xx, timeout, connect error, an unexpected exception in our own code).
* NO VALUES. Never a tool result, argument value, token or presigned URL. Ids (project,
  scenario, run) are the only argument-derived fields. Argument samples are a separate,
  capped record (``kind=sample``).
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware
from pydantic import ValidationError as PydanticValidationError

logger = logging.getLogger(__name__)

ACTOR_HEADER = "X-Hydrata-Actor"
REQUEST_ID_HEADER = "X-Request-ID"
EVENT = "mcp_tool_call"
ERROR_TEXT_MAX = 300
CLIENT_TEXT_MAX = 200
REQUEST_ID_MAX = 64
AGENT_PAUSED = "AGENT_PAUSED"

OK, REFUSED, INVALID, BROKEN = "ok", "refused", "invalid", "broken"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SECRET = re.compile(r"hk_[A-Za-z0-9_\-]{6,}|[Bb]earer\s+\S+|eyJ[\w-]+\.[\w-]+\.[\w-]*")
_ARG_VALUE = re.compile(r"input_value=.*?, input_type=")
_LOG_ONLY_LINE = re.compile(r"\n\s*For further information visit \S+")


# ---------------------------------------------------------------------------
# Per-call state: the request id we forward and the identity Django reports.
# ---------------------------------------------------------------------------
@dataclass
class CallState:
    request_id: str
    actor: dict | None = None
    # Mutable on purpose: the tool may run in a child task with a COPY of the context,
    # but it shares this object, so the actor noted in client._send reaches the record.
    replies: list = field(default_factory=list)


_state: contextvars.ContextVar[CallState | None] = contextvars.ContextVar("hydrata_mcp_call_state", default=None)


def clean_request_id(value: str | None) -> str:
    """Control characters stripped and clipped to 64 chars, as Django stores it."""
    return _CONTROL.sub("", value or "").strip()[:REQUEST_ID_MAX]


def current_request_id() -> str:
    """The id to send as ``X-Request-ID`` on the current upstream call."""
    state = _state.get()
    return state.request_id if state else uuid.uuid4().hex


def parse_actor(header: str | None) -> dict | None:
    """``X-Hydrata-Actor`` -> identity fields, or None when absent or malformed.

    The username is percent-encoded in the header; ``parse_qs`` decodes it. Nothing is
    invented for a missing or unreadable header: the caller was not verified.
    """
    if not header:
        return None
    try:
        fields = {k: v[0] for k, v in parse_qs(header, keep_blank_values=True, strict_parsing=False).items()}
        user_id = int(fields["user_id"])
        grant = int(fields["grant"]) if fields.get("grant", "").isdigit() else None
    except (KeyError, ValueError, IndexError):
        return None
    return {
        "user_id": user_id,
        "username": fields.get("username", "")[:CLIENT_TEXT_MAX],
        "grant": grant,
        "credential": fields.get("credential", "")[:20] or None,
        "internal": fields.get("internal") == "1",
    }


def note_reply(resp: httpx.Response) -> None:
    """Called for every Django reply: the FIRST reply that carries an actor header wins."""
    state = _state.get()
    if state is None or state.actor is not None:
        return
    actor = parse_actor(resp.headers.get(ACTOR_HEADER))
    if actor:
        state.actor = actor


# ---------------------------------------------------------------------------
# Credential fingerprint (identity when Django did not answer with an actor)
# ---------------------------------------------------------------------------
def _jwt_claims(token: str) -> dict:
    try:
        segment = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def credential_identity(authorization: str | None) -> dict:
    """``{fingerprint, credential, oauth_client_id}`` from the Authorization header, no verification.

    hk_ key: the id prefix (``hk_`` + 12 hex), never the secret part. JWT: a hash of ``sub``.
    """
    token = (authorization or "").strip().partition(" ")[2].strip()
    if not token:
        return {"fingerprint": None, "credential": None, "oauth_client_id": None}
    if token.startswith("hk_"):
        parts = token.split("_", 2)
        fingerprint = "_".join(parts[:2]) if len(parts) == 3 else f"hk_{_digest(token)}"
        return {"fingerprint": fingerprint, "credential": "agent_key", "oauth_client_id": None}
    claims = _jwt_claims(token)
    sub = claims.get("sub")
    client = claims.get("client_id") or claims.get("azp")
    return {
        "fingerprint": f"jwt_{_digest(str(sub))}" if sub else f"tok_{_digest(token)}",
        "credential": "workos" if claims else None,
        "oauth_client_id": _clip(client) if isinstance(client, str) else None,
    }


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------
def _clip(value, limit: int = CLIENT_TEXT_MAX) -> str | None:
    if value is None:
        return None
    return _CONTROL.sub(" ", str(value))[:limit]


def scrub(text, limit: int = ERROR_TEXT_MAX) -> str:
    """Error text safe to log: presigned URLs, tokens and pydantic's echo of argument values removed."""
    from ._helpers import _elide_presigned  # late: _helpers imports _common, which imports this module

    text = _LOG_ONLY_LINE.sub("", str(text))
    text = _ARG_VALUE.sub("input_value=<omitted>, input_type=", text)
    text = _elide_presigned(text)
    text = _SECRET.sub("<redacted>", text)
    return _CONTROL.sub(" ", text)[:limit]


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------
def _chain(exc: BaseException) -> list[BaseException]:
    """exc, then everything on its __cause__/__context__ chain (cycle-safe)."""
    seen: list[BaseException] = []
    pending = [exc]
    while pending:
        current = pending.pop(0)
        if any(current is s for s in seen):
            continue
        seen.append(current)
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return seen


def classify_status(status: int | None, code: str | None) -> str:
    if status is None:
        return INVALID
    if status == 503 and code == AGENT_PAUSED:
        return REFUSED  # the kill switch is a decision, not an outage
    if 400 <= status < 500:
        return REFUSED
    return BROKEN


def classify_exception(exc: BaseException) -> tuple[str, int | None, str | None, str]:
    """``(outcome, http_status, error_code, message)`` for an exception raised out of a tool call.

    client.py raises HydrataAPIError INSIDE ``except`` without ``from``, so the original
    httpx error sits on ``__context__``; FastMCP wraps tool exceptions as ``ToolError from e``.
    Walk the whole chain.
    """
    from .client import HydrataAPIError  # late: client imports this module

    chain = _chain(exc)
    api_errors = [e for e in chain if isinstance(e, HydrataAPIError)]
    message = str(api_errors[0] if api_errors else exc)
    for e in api_errors:
        if e.status is not None:
            return classify_status(e.status, e.code), e.status, e.code, str(e)
    for e in chain:
        if isinstance(e, httpx.HTTPStatusError):
            return classify_status(e.response.status_code, None), e.response.status_code, None, message
    code = next((e.code for e in api_errors if e.code), None)
    if any(isinstance(e, httpx.TransportError) for e in chain):
        return BROKEN, None, code, message
    for e in chain:
        if isinstance(e, (NotFoundError, FastMCPValidationError, PydanticValidationError)):
            return INVALID, None, None, message
    for e in chain:
        if not isinstance(e, (ToolError, HydrataAPIError)):
            return BROKEN, None, None, message  # an unexpected exception in our own code
    return INVALID, None, code, message


def classify_result(result) -> tuple[str, int | None, str | None, str | None]:
    """A RETURNED result with ``http_status`` >= 400 (409 CONSENT_REQUIRED, UNREVIEWED_INPUTS, 422) is a refusal."""
    content = getattr(result, "structured_content", None)
    status = content.get("http_status") if isinstance(content, dict) else None
    if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
        code = content.get("error_code")
        text = content.get("detail") or content.get("say_to_user") or content.get("message")
        return classify_status(status, code if isinstance(code, str) else None), status, code, text
    return OK, None, None, None


def _response_bytes(result) -> int:
    total = 0
    for part in getattr(result, "content", None) or []:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            total += len(text.encode())
    return total


def _int_arg(arguments, key):
    value = (arguments or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------
def _client_fields(context, headers: dict) -> dict:
    """Client name/version: the initialize clientInfo when this session saw one, else the User-Agent.

    The server runs stateless_http, so a tools/call normally arrives on a session that never saw
    ``initialize`` and clientInfo is empty; the User-Agent ("claude-code/2.1.9") is then the signal.
    """
    name = version = protocol = None
    try:
        params = context.fastmcp_context.session.client_params
        if params is not None:
            name, version = params.clientInfo.name, params.clientInfo.version
            protocol = params.protocolVersion
    except Exception:  # noqa: BLE001 - telemetry must never break a call
        pass
    user_agent = headers.get("user-agent")
    if not name and user_agent:
        head = user_agent.split()[0]
        name, _, version = head.partition("/")
    return {
        "client_name": _clip(name),
        "client_version": _clip(version) or None,
        "user_agent": _clip(user_agent),
        "mcp.protocol.version": _clip(headers.get("mcp-protocol-version") or protocol, 40),
    }


def build_record(
    *,
    tool,
    arguments,
    outcome,
    status,
    code,
    error_type,
    message,
    duration_ms,
    response_bytes,
    state: CallState,
    headers: dict,
    authorization: str | None,
    client_fields: dict,
) -> dict:
    actor = state.actor
    cred = credential_identity(authorization)
    if actor:
        identity = {**actor, "identity_source": "django", "identity": f"user:{actor['user_id']}"}
    else:
        identity = {
            "user_id": None,
            "username": None,
            "grant": None,
            "credential": cred["credential"],
            "internal": None,
            "identity_source": "credential" if cred["fingerprint"] else "none",
            "identity": cred["fingerprint"],
        }
    tool_name = _clip(tool, 100)
    return {
        "event": EVENT,
        "kind": "call",
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "tool": tool_name,
        "gen_ai.tool.name": tool_name,
        "mcp.method.name": "tools/call",
        "outcome": outcome,
        "error.type": error_type,
        "error_code": code,
        "http_status": status,
        "error": scrub(message) if message else None,
        "duration_ms": duration_ms,
        "response_bytes": response_bytes,
        **client_fields,
        "oauth_client_id": cred["oauth_client_id"],
        "request_id": state.request_id,
        **identity,
        "project_id": _int_arg(arguments, "project_id"),
        "scenario_id": _int_arg(arguments, "scenario_id"),
        "run_id": _int_arg(arguments, "run_id"),
    }


def emit(record: dict) -> None:
    """One JSON line on stdout (journald -> promtail). ASCII-only, so one line stays one line."""
    sys.stdout.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class ToolCallRecorder(Middleware):
    """FastMCP middleware: exactly one ``mcp_tool_call`` line per tools/call, whatever happens."""

    async def on_call_tool(self, context, call_next):
        try:
            headers = get_http_headers(include={"authorization", "x-request-id", "user-agent", "mcp-protocol-version"})
        except Exception:  # noqa: BLE001
            headers = {}
        state = CallState(request_id=clean_request_id(headers.get("x-request-id")) or uuid.uuid4().hex)
        token = _state.set(state)
        started = time.monotonic()
        tool = getattr(context.message, "name", None)
        arguments = getattr(context.message, "arguments", None)
        outcome, status, code, error_type, message = OK, None, None, None, None
        try:
            result = await call_next(context)
        except BaseException as exc:
            outcome, status, code, message = classify_exception(exc)
            error_type = type(exc).__name__
            self._record(
                context, tool, arguments, state, headers, started, outcome, status, code, error_type, message, 0
            )
            raise
        finally:
            _state.reset(token)
        outcome, status, code, message = classify_result(result)
        if outcome != OK:
            error_type = f"http_{status}"
        self._record(
            context, tool, arguments, state, headers, started, outcome, status, code, error_type, message,
            _response_bytes(result),
        )  # fmt: skip
        return result

    @staticmethod
    def _record(context, tool, arguments, state, headers, started, outcome, status, code, error_type, message, size):
        try:
            record = build_record(
                tool=tool,
                arguments=arguments,
                outcome=outcome,
                status=status,
                code=code,
                error_type=error_type,
                message=message,
                duration_ms=int((time.monotonic() - started) * 1000),
                response_bytes=size,
                state=state,
                headers=headers,
                authorization=headers.get("authorization"),
                client_fields=_client_fields(context, headers),
            )
            emit(record)
        except Exception:  # noqa: BLE001 - a telemetry bug must never fail the customer's call
            logger.exception("mcp_tool_call record failed")
