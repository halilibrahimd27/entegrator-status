"""Logging setup for the web process: access logs never contain one-time tokens.

Verification, password-reset and invitation links carry a token in the URL path. The
access log would otherwise write it to disk (and to any log shipper).
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from uvicorn.config import LOGGING_CONFIG

TOKEN_PATHS = re.compile(r"(/(?:dogrula|sifre-sifirla|davet)/)[^/?#\s\"]+")


def redact(text: str) -> str:
    return TOKEN_PATHS.sub(r"\1[redacted]", text)


class RedactTokenPaths(logging.Filter):
    """Rewrites uvicorn access records (``args[2]`` is the request path)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def uvicorn_log_config() -> dict[str, Any]:
    config: dict[str, Any] = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_tokens"] = {"()": RedactTokenPaths}
    config["handlers"]["access"]["filters"] = ["redact_tokens"]
    return config
