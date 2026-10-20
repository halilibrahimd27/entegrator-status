"""Probe node API: signed job list and result ingest (JSON only, no cookies, no CSRF)."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from sentinel.probe.nodes import IngestPayload, ReplayError
from sentinel.probe.signing import (
    HEADER_BATCH,
    HEADER_NODE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    MAX_SKEW_S,
    verify,
)
from sentinel.web.services import services

router = APIRouter(prefix="/ingest/v1")


class NodeAuthError(Exception):
    pass


async def signed_node(request: Request) -> uuid.UUID:
    """Bearer token + timestamped HMAC over the batch id and the raw body."""
    headers = request.headers
    scheme, _, token = headers.get("authorization", "").partition(" ")
    try:
        node_id = uuid.UUID(headers.get(HEADER_NODE, ""))
        timestamp = int(headers.get(HEADER_TIMESTAMP, ""))
    except ValueError:
        raise NodeAuthError from None
    if scheme.lower() != "bearer" or not token or abs(time.time() - timestamp) > MAX_SKEW_S:
        raise NodeAuthError
    svc = services(request)
    if not await svc.nodes.authenticate(node_id, token):
        raise NodeAuthError
    body = await request.body()
    batch = headers.get(HEADER_BATCH, "-")
    if not verify(
        svc.nodes.signing_key(node_id), timestamp, batch, body, headers.get(HEADER_SIGNATURE, "")
    ):
        raise NodeAuthError
    return node_id


Node = Annotated[uuid.UUID, Depends(signed_node)]


@router.get("/jobs")
async def jobs(request: Request, node: Node) -> dict[str, Any]:
    return {"jobs": [asdict(job) for job in await services(request).nodes.jobs()]}


@router.post("/results", status_code=202, response_model=None)
async def results(request: Request, node: Node) -> dict[str, int] | JSONResponse:
    try:
        batch_id = uuid.UUID(request.headers.get(HEADER_BATCH, ""))
        payload = IngestPayload.model_validate_json(await request.body())
        accepted = await services(request).nodes.ingest(node, batch_id, payload)
    except ValidationError as exc:
        problems = [
            {"loc": e["loc"], "msg": e["msg"]}
            for e in exc.errors(include_input=False, include_url=False, include_context=False)
        ]
        return JSONResponse({"error": "invalid", "detail": problems}, 422)
    except ValueError as exc:
        return JSONResponse({"error": "invalid", "detail": str(exc)}, 422)
    except ReplayError:
        return JSONResponse({"error": "replayed batch"}, 409)
    return {"accepted": accepted}


async def unauthorized(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)
