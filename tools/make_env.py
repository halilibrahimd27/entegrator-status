"""Create a local ``.env`` from ``.env.example`` with fresh random secrets.

Development convenience only. An existing file is never overwritten: keys that a newer
version needs (a new secret, say) are appended with fresh values, and nothing already
in the file changes.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED = {
    "SESSION_KEY",
    "TOKEN_HASH_KEY",
    "WEBHOOK_SIGNING_KEY",
    "INGEST_HMAC_KEY",
    "TOTP_ENCRYPTION_KEY",
    "CREDENTIALS_MASTER_KEY",
    "POSTGRES_PASSWORD",
}
DEV_VALUES = {
    "PUBLIC_BASE_URL": "http://localhost:8000",
    "CONTACT_EMAIL": "dev@entegrator-status.invalid",
    # compose.yml runs mailpit (UI on http://127.0.0.1:8025)
    "MAIL_FROM": "bildirim@entegrator-status.invalid",
    "SMTP_HOST": "mailpit",
    "SMTP_PORT": "1025",
    "SMTP_SECURITY": "none",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
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


def missing_lines(existing: str, example: str) -> list[str]:
    """Lines of the rendered example whose key the existing file does not set."""
    present = {line.partition("=")[0].strip() for line in existing.splitlines() if "=" in line}
    return [
        line
        for line in render(example).splitlines()
        if "=" in line and not line.startswith("#") and line.partition("=")[0] not in present
    ]


def main() -> int:
    target = ROOT / ".env"
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    if target.exists():
        added = missing_lines(target.read_text(encoding="utf-8"), example)
        if not added:
            print(".env is complete; nothing changed.")
            return 0
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n# added by make env\n" + "\n".join(added) + "\n")
        print("Added to .env: " + ", ".join(line.partition("=")[0] for line in added))
        return 0
    content = render(example)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    print("Wrote .env (mode 600) with fresh secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
