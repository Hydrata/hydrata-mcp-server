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

    async def _send(
        self, method: str, url: str, path: str, raise_for_status: bool = True, **kwargs
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
            if raise_for_status or resp.is_server_error:
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
        self, path: str, json: dict | None = None, raise_for_status: bool = True
    ) -> tuple[Any, int]:
        """POST request. Returns (body, status_code) since some endpoints return 202.

        With ``raise_for_status=False`` a 4xx is returned as ``(body, status)``
        too (TASK-3172: build_scenario surfaces 422/409 bodies verbatim);
        the default keeps every other tool's raise-on-4xx behaviour.
        """
        resp = await self._send(
            "POST", f"{self._base}{path}", path, raise_for_status=raise_for_status, json=json or {}
        )
        return self._body(resp), resp.status_code

    async def patch(self, path: str, json: dict | None = None) -> tuple[Any, int]:
        """PATCH request (partial update). Returns (body, status_code) like post().

        TASK-3171 (W1.2, epic 2467) — attach_input_layer sets ONE field
        (``gn_layer``) on one of the SIX default input rows. It is the only
        safe write: the four list+retrieve+update viewsets accept nothing
        else, and a POST to the two create-capable ones (structures,
        mesh-regions) has its gn_layer overwritten by the async layer factory.
        """
        resp = await self._send("PATCH", f"{self._base}{path}", path, json=json or {})
        return self._body(resp), resp.status_code
