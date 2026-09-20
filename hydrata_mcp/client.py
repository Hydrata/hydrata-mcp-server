"""Async HTTP client wrapper for Hydrata REST API."""

from typing import Any

import httpx
from fastmcp.server.dependencies import get_http_headers

from .config import Config


class HydrataAPIError(Exception):
    """Raised when the Hydrata API is unreachable or returns an error.

    FastMCP catches this and returns ``isError: true`` with the message,
    giving agents a clean signal instead of a raw traceback.
    """


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


class HydrataClient:
    """Thin async wrapper around the Hydrata /api/v2/anuga/ endpoints.

    Holds NO credential of its own. Each request forwards the calling MCP
    client's ``Authorization`` header verbatim (TASK-3166, W0.1, epic 2467),
    so GeoNode scopes results to the real user and the audit trail names them.
    Returns parsed JSON dicts.
    """

    def __init__(self, config: Config) -> None:
        self._base = config.api_url
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
        """Per-request headers: the caller's Authorization, plus Host when configured.

        TASK-3166 (W0.1, epic 2467) — ``get_http_headers()`` STRIPS ``authorization``
        (and ``cookie``) unless it is named in ``include``; the bare call would
        silently send an anonymous upstream request. Outside an HTTP request
        (direct tool invocation, tests) it returns ``{}`` and nothing is forwarded.
        Only Authorization is picked out — the rest of the inbound headers
        (user-agent, accept-language, …) are the MCP client's, not ours to relay.
        """
        headers: dict[str, str] = {}
        auth = get_http_headers(include={"authorization"}).get("authorization")
        if auth:
            headers["Authorization"] = auth
        if self._api_host:
            headers["Host"] = self._api_host
        return headers

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get(self, path: str, params: dict | None = None) -> Any:
        client = await self._ensure_client()
        url = f"{self._base}{path}"
        try:
            resp = await client.get(url, params=params, headers=self._request_headers())
            resp.raise_for_status()
        except httpx.ConnectError:
            raise HydrataAPIError(
                f"Hydrata API is unreachable at {self._base}. The backend may be restarting."
            )
        except httpx.TimeoutException:
            raise HydrataAPIError(
                "Hydrata API request timed out after 30s. Try again shortly."
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status >= 500:
                raise HydrataAPIError(
                    f"Hydrata API server error ({status}). The backend may be experiencing issues."
                )
            raise HydrataAPIError(_client_error_message(exc, "GET", path))
        return resp.json()

    async def post(self, path: str, json: dict | None = None) -> tuple[Any, int]:
        """POST request. Returns (body, status_code) since some endpoints return 202."""
        client = await self._ensure_client()
        url = f"{self._base}{path}"
        try:
            resp = await client.post(url, json=json or {}, headers=self._request_headers())
            resp.raise_for_status()
        except httpx.ConnectError:
            raise HydrataAPIError(
                f"Hydrata API is unreachable at {self._base}. The backend may be restarting."
            )
        except httpx.TimeoutException:
            raise HydrataAPIError(
                "Hydrata API request timed out after 30s. Try again shortly."
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status >= 500:
                raise HydrataAPIError(
                    f"Hydrata API server error ({status}). The backend may be experiencing issues."
                )
            raise HydrataAPIError(_client_error_message(exc, "POST", path))
        body = resp.json() if resp.content else {}
        return body, resp.status_code
