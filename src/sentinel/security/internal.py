"""Client for the prober → core channel.

The target is operator-configured (``PROBE_INGEST_URL``), not user-entered, and on the
compose network it is a private ``http`` address, so the SSRF guard does not apply.
It still lives in ``sentinel.security`` because only this package may import httpx.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from sentinel.probe.signing import (
    HEADER_BATCH,
    HEADER_NODE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    sign,
)


class CoreUnavailableError(Exception):
    """Network error or 5xx: keep the results and retry later."""


class CoreRejectedError(Exception):
    """4xx other than a replay: the request itself is wrong; retrying will not help."""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(f"HTTP {status_code}{': ' + message if message else ''}")
        self.status_code = status_code


class CoreClient:
    def __init__(
        self,
        base_url: str,
        node_id: uuid.UUID,
        token: str,
        signing_key: str,
        *,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._node_id = node_id
        self._token = token
        self._key = signing_key
        self._clock = clock
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=httpx.Timeout(15.0),
            trust_env=False,
            headers={"User-Agent": user_agent},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, batch: str, body: bytes) -> dict[str, str]:
        timestamp = int(self._clock())
        return {
            "Authorization": f"Bearer {self._token}",
            HEADER_NODE: str(self._node_id),
            HEADER_TIMESTAMP: str(timestamp),
            HEADER_BATCH: batch,
            HEADER_SIGNATURE: sign(self._key, timestamp, batch, body),
        }

    async def _send(self, method: str, path: str, batch: str, body: bytes) -> httpx.Response:
        headers = self._headers(batch, body)
        if body:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._client.request(method, path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise CoreUnavailableError(type(exc).__name__) from exc
        if response.status_code >= 500:
            raise CoreUnavailableError(f"HTTP {response.status_code}")
        return response

    async def jobs(self) -> list[dict[str, Any]]:
        response = await self._send("GET", "/ingest/v1/jobs", "-", b"")
        if response.status_code != 200:
            raise CoreRejectedError(response.status_code)
        jobs: list[dict[str, Any]] = response.json()["jobs"]
        return jobs

    async def send(self, batch_id: uuid.UUID, results: list[dict[str, Any]]) -> int:
        """Ship one batch. A replay answer means an earlier attempt already landed."""
        body = json.dumps({"results": results}, default=str).encode()
        response = await self._send("POST", "/ingest/v1/results", str(batch_id), body)
        if response.status_code == 409:
            return 0
        if response.status_code != 202:
            raise CoreRejectedError(response.status_code, response.text[:200])
        accepted: int = response.json()["accepted"]
        return accepted
