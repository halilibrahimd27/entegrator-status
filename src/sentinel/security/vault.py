"""Envelope encryption for organization credentials (ADR 0011). AES-256-GCM throughout.

master key ──HKDF──▶ wrapping key ──wraps──▶ one data key per organization
                                              └─seals─▶ each secret of that organization

Associated data binds every ciphertext to its place: a data key to its organization,
a secret to its organization and credential. A blob copied onto another row fails to
decrypt instead of decrypting to someone else's secret.

Job sealing: a node's job key is derived from its signing key, so the core can seal a
secret for exactly one node and the node can open it without any new key material.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_VERSION = b"\x01"
_NONCE = 12


class VaultError(Exception):
    """A blob could not be opened (wrong key, wrong row, or tampered)."""


def _derive(key: bytes, info: bytes) -> AESGCM:
    return AESGCM(HKDF(algorithm=SHA256(), length=32, salt=None, info=info).derive(key))


def _seal(aead: AESGCM, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    return _VERSION + nonce + aead.encrypt(nonce, plaintext, aad)


def _open(aead: AESGCM, blob: bytes, aad: bytes) -> bytes:
    if blob[:1] != _VERSION or len(blob) < 1 + _NONCE + 16:
        raise VaultError("unknown format")
    try:
        return aead.decrypt(blob[1 : 1 + _NONCE], blob[1 + _NONCE :], aad)
    except InvalidTag as exc:
        raise VaultError("cannot decrypt") from exc


class Vault:
    def __init__(self, master_key: str) -> None:
        self._wrap = _derive(master_key.encode(), b"entegrator-status/data-keys/v1")

    @staticmethod
    def new_data_key() -> bytes:
        return AESGCM.generate_key(bit_length=256)

    def wrap(self, data_key: bytes, org_id: uuid.UUID) -> bytes:
        return _seal(self._wrap, data_key, b"org:" + org_id.bytes)

    def unwrap(self, wrapped: bytes, org_id: uuid.UUID) -> bytes:
        return _open(self._wrap, wrapped, b"org:" + org_id.bytes)

    @staticmethod
    def seal_secret(
        data_key: bytes, secret: dict[str, str], org_id: uuid.UUID, credential_id: uuid.UUID
    ) -> bytes:
        aad = b"credential:" + org_id.bytes + credential_id.bytes
        return _seal(AESGCM(data_key), json.dumps(secret).encode(), aad)

    @staticmethod
    def open_secret(
        data_key: bytes, blob: bytes, org_id: uuid.UUID, credential_id: uuid.UUID
    ) -> dict[str, str]:
        aad = b"credential:" + org_id.bytes + credential_id.bytes
        value: dict[str, str] = json.loads(_open(AESGCM(data_key), blob, aad))
        return value


def job_box(signing_key: str) -> AESGCM:
    """The key that seals job secrets for one node (both sides derive it)."""
    return _derive(signing_key.encode(), b"entegrator-status/job-secrets/v1")


def seal_for_node(signing_key: str, job_key: str, payload: dict[str, Any]) -> str:
    return _seal(job_box(signing_key), json.dumps(payload).encode(), job_key.encode()).hex()


def open_on_node(signing_key: str, job_key: str, sealed: str) -> dict[str, Any]:
    try:
        blob = bytes.fromhex(sealed)
    except ValueError as exc:
        raise VaultError("unknown format") from exc
    value: dict[str, Any] = json.loads(_open(job_box(signing_key), blob, job_key.encode()))
    return value
