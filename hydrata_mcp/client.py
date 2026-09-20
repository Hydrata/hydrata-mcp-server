"""Async HTTP client wrapper for Hydrata REST API."""

from typing import Any
from urllib.parse import urlsplit

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

    async def _send(self, method: str, url: str, path: str, **kwargs) -> httpx.Response:
        """One request with the caller's headers; every failure becomes a HydrataAPIError.

        `path` is only for the message (the part after the base, as the tool
        wrote it). 5xx bodies are deliberately NOT relayed — they are HTML
        tracebacks, not a signal the agent can act on.
        """
        client = await self._ensure_client()
        try:
            resp = await client.request(method, url, headers=self._request_headers(), **kwargs)
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
            raise HydrataAPIError(_client_error_message(exc, method, path))
        return resp

    @staticmethod
    def _body(resp: httpx.Response) -> Any:
        return resp.json() if resp.content else {}

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

    async def post(self, path: str, json: dict | None = None) -> tuple[Any, int]:
        """POST request. Returns (body, status_code) since some endpoints return 202."""
        resp = await self._send("POST", f"{self._base}{path}", path, json=json or {})
        return self._body(resp), resp.status_code

    async def patch(self, path: str, json: dict | None = None) -> tuple[Any, int]:
        """PATCH request (partial update). Returns (body, status_code) like post().

        TASK-3171 (W1.2, epic 2467) — attach_input_layer sets ONE field
        (``gn_layer``) on a default input row; PATCH is the only write the
        four list+retrieve+update viewsets accept.
        """
        resp = await self._send("PATCH", f"{self._base}{path}", path, json=json or {})
        return self._body(resp), resp.status_code
