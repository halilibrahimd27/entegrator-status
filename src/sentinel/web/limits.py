"""Request body size limit (pure ASGI).

Starlette spools multipart file parts to disk without a limit; this caps every request
body before any parser sees it: by ``Content-Length`` when present, by counting bytes
otherwise.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 3 * 1024 * 1024  # imports accept 2 MB; forms are far smaller


class BodyTooLargeError(Exception):
    pass


class BodySizeLimit:
    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                await _reject(send)
                return
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise BodyTooLargeError
            return message

        try:
            await self.app(scope, limited, send)
        except BodyTooLargeError:
            await _reject(send)


async def _reject(send: Send) -> None:
    body = "İstek çok büyük.".encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
