"""Async HTTP client wrapper for Hydrata REST API."""

from typing import Any

import httpx

from .config import Config


class HydrataAPIError(Exception):
    """Raised when the Hydrata API is unreachable or returns an error.

    FastMCP catches this and returns ``isError: true`` with the message,
    giving agents a clean signal instead of a raw traceback.
    """


class HydrataClient:
    """Thin async wrapper around the Hydrata /api/v2/anuga/ endpoints.

    Uses HTTP Basic authentication and returns parsed JSON dicts.
    """

    def __init__(self, config: Config) -> None:
        self._base = config.api_url
        self._auth = httpx.BasicAuth(config.api_username, config.api_password)
        self._headers = {"Accept": "application/json"}
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                auth=self._auth,
                headers=self._headers,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get(self, path: str, params: dict | None = None) -> Any:
        client = await self._ensure_client()
        url = f"{self._base}{path}"
        try:
            resp = await client.get(url, params=params)
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
            raise HydrataAPIError(
                f"Hydrata API returned {status}: {exc.response.reason_phrase} for GET {path}"
            )
        return resp.json()

    async def post(self, path: str, json: dict | None = None) -> tuple[Any, int]:
        """POST request. Returns (body, status_code) since some endpoints return 202."""
        client = await self._ensure_client()
        url = f"{self._base}{path}"
        try:
            resp = await client.post(url, json=json or {})
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
            raise HydrataAPIError(
                f"Hydrata API returned {status}: {exc.response.reason_phrase} for POST {path}"
            )
        body = resp.json() if resp.content else {}
        return body, resp.status_code
