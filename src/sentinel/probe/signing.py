"""Node credentials and request signatures (shared by the core and the prober).

Each node has a random token (stored hashed by the core) and a signing key derived from
``INGEST_HMAC_KEY`` and the node id. Remote nodes receive their own key only; they
never hold the master key, so one compromised node cannot sign for another.

Signature: ``sha256=HMAC(key, f"{timestamp}.{batch_id}.".encode() + body)``.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

MAX_SKEW_S = 300
HEADER_NODE = "x-sentinel-node"
HEADER_TIMESTAMP = "x-sentinel-timestamp"
HEADER_BATCH = "x-sentinel-batch"
HEADER_SIGNATURE = "x-sentinel-signature"


def node_signing_key(ingest_hmac_key: str, node_id: uuid.UUID) -> str:
    return hmac.new(
        ingest_hmac_key.encode(), b"node-signing\x00" + node_id.bytes, hashlib.sha256
    ).hexdigest()


def sign(key: str, timestamp: int, batch_id: str, body: bytes) -> str:
    message = f"{timestamp}.{batch_id}.".encode() + body
    return "sha256=" + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def verify(key: str, timestamp: int, batch_id: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign(key, timestamp, batch_id, body), signature)
