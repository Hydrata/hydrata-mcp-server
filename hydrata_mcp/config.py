"""Configuration from environment variables."""

import os
from dataclasses import dataclass


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
            data_envelope=os.environ.get("HYDRATA_MCP_DATA_ENVELOPE", "on").strip().lower()
            not in ("off", "0", "false", "no"),
        )
