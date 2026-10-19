"""Create a local ``.env`` from ``.env.example`` with fresh random secrets.

Development convenience only; it never overwrites an existing file.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED = {
    "SESSION_KEY",
    "TOKEN_HASH_KEY",
    "WEBHOOK_SIGNING_KEY",
    "INGEST_HMAC_KEY",
    "POSTGRES_PASSWORD",
}
DEV_VALUES = {
    "PUBLIC_BASE_URL": "http://localhost:8000",
    "CONTACT_EMAIL": "dev@entegrator-status.invalid",
}


def render(example: str) -> str:
    lines = []
    for line in example.splitlines():
        key, sep, _ = line.partition("=")
        if sep and key in GENERATED:
            line = f"{key}={secrets.token_urlsafe(48)}"
        elif sep and key in DEV_VALUES:
            line = f"{key}={DEV_VALUES[key]}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> int:
    target = ROOT / ".env"
    if target.exists():
        print(".env already exists; leaving it alone.", file=sys.stderr)
        return 1
    content = render((ROOT / ".env.example").read_text(encoding="utf-8"))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    print("Wrote .env (mode 600) with fresh secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
