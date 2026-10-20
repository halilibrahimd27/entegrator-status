"""Envelope encryption and job sealing (ADR 0011)."""

from __future__ import annotations

import secrets
import uuid

import pytest

from sentinel.security.vault import Vault, VaultError, open_on_node, seal_for_node

ORG, OTHER = uuid.uuid4(), uuid.uuid4()
CRED = uuid.uuid4()


def test_data_keys_are_bound_to_their_organization() -> None:
    vault = Vault(secrets.token_urlsafe(48))
    key = vault.new_data_key()
    wrapped = vault.wrap(key, ORG)
    assert key not in wrapped
    assert vault.unwrap(wrapped, ORG) == key
    with pytest.raises(VaultError):
        vault.unwrap(wrapped, OTHER)  # copied onto another organization's row
    with pytest.raises(VaultError):
        Vault(secrets.token_urlsafe(48)).unwrap(wrapped, ORG)  # another master key


def test_secrets_are_bound_to_their_row_and_tamper_evident() -> None:
    key = Vault.new_data_key()
    canary = "canary-" + secrets.token_hex(8)
    blob = Vault.seal_secret(key, {"token": canary}, ORG, CRED)
    assert canary.encode() not in blob
    assert Vault.open_secret(key, blob, ORG, CRED) == {"token": canary}
    for org, cred in ((OTHER, CRED), (ORG, uuid.uuid4())):
        with pytest.raises(VaultError):
            Vault.open_secret(key, blob, org, cred)
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(VaultError):
        Vault.open_secret(key, tampered, ORG, CRED)
    with pytest.raises(VaultError):
        Vault.open_secret(key, b"\x02" + blob[1:], ORG, CRED)  # unknown version


def test_job_secrets_open_only_on_their_node_and_job() -> None:
    node_key, other_node = secrets.token_hex(32), secrets.token_hex(32)
    sealed = seal_for_node(node_key, "job-1", {"header": "X-Api-Key", "value": "s3cr3t-value"})
    assert "s3cr3t-value" not in sealed
    assert open_on_node(node_key, "job-1", sealed)["value"] == "s3cr3t-value"
    with pytest.raises(VaultError):
        open_on_node(other_node, "job-1", sealed)
    with pytest.raises(VaultError):
        open_on_node(node_key, "job-2", sealed)  # moved to another job
    with pytest.raises(VaultError):
        open_on_node(node_key, "job-1", "not hex")
