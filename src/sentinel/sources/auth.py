"""Node side of authenticated checks (ADR 0011).

A job of an authenticated source carries its secret sealed for this node. ``AuthProvider``
opens it in memory when the request is made and turns it into a header bound to the
credential's host. OAuth2 client credentials are exchanged for an access token at the
configured token URL (a flagged POST, no redirects) and the token is cached in memory
until a minute before it expires. Nothing here is written to disk or logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from sentinel.security.errors import OutboundError
from sentinel.security.http import BoundCredential, FetchPolicy, OutboundClient, Timeouts
from sentinel.security.vault import VaultError, open_on_node

TOKEN_MARGIN_S = 60.0
MAX_TOKEN_TTL_S = 24 * 3600.0


class AuthUnavailableError(Exception):
    """The secret could not be opened or no token could be obtained."""


@dataclass
class _Token:
    value: str
    expires: float


class AuthProvider:
    def __init__(
        self,
        signing_key: str,
        http: OutboundClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = signing_key
        self._http = http
        self._clock = clock
        self._tokens: dict[str, _Token] = {}

    async def credential_for(self, job_key: str, sealed: str | None) -> BoundCredential | None:
        if not sealed:
            return None
        try:
            payload = open_on_node(self._key, job_key, sealed)
        except VaultError as exc:
            raise AuthUnavailableError("cannot open the job secret") from exc
        host = str(payload["host"])
        kind = payload["kind"]
        secret: dict[str, str] = payload["secret"]
        if kind == "api_key_header":
            return BoundCredential(host, str(payload["header_name"]), secret["value"])
        if kind == "bearer":
            return BoundCredential(host, "Authorization", f"Bearer {secret['token']}")
        if kind == "basic":
            return BoundCredential(
                host, "Authorization", _basic(secret["username"], secret["password"])
            )
        if kind == "oauth2_client":
            token = await self._token(payload, secret)
            return BoundCredential(host, "Authorization", f"Bearer {token}")
        raise AuthUnavailableError("unknown credential kind")

    async def _token(self, payload: dict[str, Any], secret: dict[str, str]) -> str:
        url = str(payload["token_url"])
        cache = hashlib.sha256(f"{url}\x00{secret['client_id']}".encode()).hexdigest()
        cached = self._tokens.get(cache)
        if cached is not None and cached.expires > self._clock():
            return cached.value
        form = {"grant_type": "client_credentials"}
        if payload.get("scope"):
            form["scope"] = str(payload["scope"])
        token_host = urlsplit(url).hostname or ""
        policy = FetchPolicy(
            method="POST",
            form_body=urlencode(form).encode(),
            token_request=True,
            max_redirects=0,
            max_bytes=64 * 1024,
            timeouts=Timeouts(5, 10, 15),
            headers={"Accept": "application/json"},
            credential=BoundCredential(
                token_host, "Authorization", _basic(secret["client_id"], secret["client_secret"])
            ),
        )
        try:
            result = await self._http.fetch(url, policy)
        except OutboundError as exc:
            raise AuthUnavailableError(f"token request failed: {exc.kind}") from exc
        if result.status_code != 200:
            raise AuthUnavailableError(f"token request answered HTTP {result.status_code}")
        try:
            data = json.loads(result.body)
            access = str(data["access_token"])
            ttl = float(data.get("expires_in") or 300)
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthUnavailableError("unreadable token answer") from exc
        ttl = min(max(ttl - TOKEN_MARGIN_S, 0.0), MAX_TOKEN_TTL_S)
        self._tokens[cache] = _Token(access, self._clock() + ttl)
        return access


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
