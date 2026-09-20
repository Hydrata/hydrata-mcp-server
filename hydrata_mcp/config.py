"""Configuration from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """MCP server configuration loaded from environment."""

    api_url: str
    api_username: str
    api_password: str
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

        api_username = os.environ.get("HYDRATA_API_USERNAME", "")
        api_password = os.environ.get("HYDRATA_API_PASSWORD", "")
        if not api_username or not api_password:
            raise RuntimeError(
                "HYDRATA_API_USERNAME and HYDRATA_API_PASSWORD environment variables are required"
            )

        return cls(
            api_url=api_url.rstrip("/"),
            api_username=api_username,
            api_password=api_password,
            port=int(os.environ.get("HYDRATA_MCP_PORT", "8001")),
            host=os.environ.get("HYDRATA_MCP_HOST", "127.0.0.1"),
            api_host=os.environ.get("HYDRATA_API_HOST", ""),
        )
