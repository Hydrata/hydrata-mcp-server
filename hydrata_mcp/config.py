"""Configuration from environment variables."""

import os
from dataclasses import dataclass

# TASK-4177 — User-Agent substrings (matched case-insensitively) of the MCP catalog
# crawlers that may read the tool catalog without credentials. See RequireAuth in
# server.py and docs/security-model.md. mcptry.com's indexer and Smithery's
# scanner; no real MCP client (claude.ai, ChatGPT, Claude Code, Cursor, VS Code)
# sends either.
DEFAULT_DISCOVERY_USER_AGENTS = ("mcptry.com indexer", "SmitheryBot")


def _discovery_user_agents(raw: str | None) -> tuple[str, ...]:
    """Parse HYDRATA_MCP_DISCOVERY_USER_AGENTS: unset → the default; comma-separated;
    blanks dropped, so an explicitly EMPTY value disables anonymous discovery."""
    if raw is None:
        return tuple(ua.lower() for ua in DEFAULT_DISCOVERY_USER_AGENTS)
    return tuple(ua.strip().lower() for ua in raw.split(",") if ua.strip())


@dataclass(frozen=True)
class Config:
    """MCP server configuration loaded from environment.

    TASK-3168 (W0.3, epic 2467) — D2: the server holds NO identity. The former
    server-held username/password fields are gone, not optional: since W0.1
    every upstream request carries the caller's own ``Authorization`` header
    (since TASK-3508 an ``hk_`` agent key, verified by Django),
    and a shared account was a latent cliff (any project granted to it would
    have been inherited by every caller).
    """

    api_url: str
    port: int = 8001
    host: str = "127.0.0.1"
    # TASK-3166 (W0.1, epic 2467) — Host header sent upstream when set. On prod the
    # API URL points at an internal nginx block (127.0.0.1:8081) whose server_name
    # is the public hostname, so the request must carry `Host: hydrata.com` even
    # though the URL host is 127.0.0.1. Unset on localhost: httpx derives Host
    # from the URL.
    api_host: str = ""
    # TASK-3203 (W1, epic 3200) — the origin a HUMAN opens a ui_url hand-back
    # link on. Unset: https://<api_host> when api_host is set (prod: the API URL
    # is the loopback 127.0.0.1:8081 listener, which no browser can reach), else
    # the API URL's own origin. Localhost's hot-reload dev server sets
    # HYDRATA_UI_ORIGIN=http://localhost:8081.
    ui_origin: str = ""
    # TASK-3511 (W2b, epic 3200, D32) — wrap people's text in tool results as
    # <untrusted-data>…</untrusted-data>. ON unless HYDRATA_MCP_DATA_ENVELOPE is
    # off/0/false/no: the OFF switch exists ONLY for the MCP harness's negative
    # control (merewether-injected run with the envelope disabled must be able
    # to fail). Never set it on a served site.
    data_envelope: bool = True
    # TASK-3512 (W2e, epic 3200) — the canonical MCP resource URL, WITH the trailing
    # slash (https://hydrata.com/mcp/). Set: a 401 names the protected-resource
    # metadata (RFC 9728 / MCP authorization spec) and a Bearer JWT from the
    # one-click connect flow is passed through to Django, which verifies it.
    # Unset (the default): hk_ agent keys only, exactly as before.
    resource_url: str = ""
    # TASK-4177 — lower-cased User-Agent substrings whose ANONYMOUS requests may
    # read the catalog (initialize, notifications/initialized, ping, tools/list;
    # never tools/call). HYDRATA_MCP_DISCOVERY_USER_AGENTS, comma-separated;
    # unset = DEFAULT_DISCOVERY_USER_AGENTS; set but empty = off (every
    # anonymous request 401s, as before TASK-4177).
    discovery_user_agents: tuple[str, ...] = tuple(ua.lower() for ua in DEFAULT_DISCOVERY_USER_AGENTS)

    @classmethod
    def from_env(cls) -> "Config":
        api_url = os.environ.get("HYDRATA_API_URL", "")
        if not api_url:
            raise RuntimeError("HYDRATA_API_URL environment variable is required")

        return cls(
            api_url=api_url.rstrip("/"),
            port=int(os.environ.get("HYDRATA_MCP_PORT", "8001")),
            host=os.environ.get("HYDRATA_MCP_HOST", "127.0.0.1"),
            api_host=os.environ.get("HYDRATA_API_HOST", ""),
            ui_origin=os.environ.get("HYDRATA_UI_ORIGIN", "").rstrip("/"),
            resource_url=os.environ.get("HYDRATA_MCP_RESOURCE_URL", "").strip(),
            data_envelope=os.environ.get("HYDRATA_MCP_DATA_ENVELOPE", "on").strip().lower()
            not in ("off", "0", "false", "no"),
            discovery_user_agents=_discovery_user_agents(os.environ.get("HYDRATA_MCP_DISCOVERY_USER_AGENTS")),
        )
