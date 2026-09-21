"""Configuration from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """MCP server configuration loaded from environment.

    TASK-3168 (W0.3, epic 2467) — D2: the server holds NO identity. The former
    server-held username/password fields are gone, not optional: since W0.1
    every upstream request carries the caller's own ``Authorization`` header,
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
        )
