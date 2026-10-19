"""Shared fixtures. Tests never touch the real network (enforced below)."""

from __future__ import annotations

import secrets
import socket
from typing import Any

import pytest

from sentinel.config import SECRET_FIELDS

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if self.family == socket.AF_UNIX or (
            isinstance(address, tuple) and address[0] in _LOOPBACK
        ):
            real_connect(self, address)
            return
        raise RuntimeError(f"tests must not use the network (connect {address!r})")

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is None or host in _LOOPBACK:
            return real_getaddrinfo(host, *args, **kwargs)
        raise RuntimeError(f"tests must not use the network (resolve {host!r})")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


def random_secret() -> str:
    return secrets.token_urlsafe(48)


@pytest.fixture
def valid_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A complete, valid environment; tests break one variable at a time."""
    env = {
        "PUBLIC_BASE_URL": "https://status.example.invalid/",
        "CONTACT_EMAIL": "bot@status.example.invalid",
        **{name.upper(): random_secret() for name in SECRET_FIELDS},
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env
