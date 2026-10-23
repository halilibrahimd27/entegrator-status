# legacy/api-sentinel — reference code

Selected files from the maintainer's earlier project `api-sentinel`
(source commit `eddf372640a138f0174f7e765c8702a92a8ff014`; the repository was deleted
on 2026-10-19). Kept so later phases do not start from a blank page.

**This directory is reference material, not part of the product.** It is not
packaged, not linted, not type-checked, not tested and must never be imported.

## Porting rule

Port a piece only in the phase that needs it. Rewrite it into `src/sentinel/`
under the rules in `CLAUDE.md` (typed, tested, outbound traffic through
`sentinel.security.http`), then delete the legacy file in the same change. When
this directory is empty, delete it.

## Inventory

| File | Phase | Reuse | Known problems to fix while porting |
|---|---|---|---|
| `alerts/base.py` | 9 | Channel interface (`send`, `validate_config`) | Untyped `details: dict[str, Any]` bag; replace with a typed event model. |
| `alerts/webhook.py` | 9 | Retry with exponential backoff, HMAC-SHA256 header name `X-Sentinel-Signature` | No SSRF guard. Signs `json.dumps(sort_keys=True)` but sends `json=payload` (different bytes), so receivers cannot verify. No timestamp in the signature (replayable). Logs the full webhook URL. No auto-disable after repeated failures. |
| `alerts/email_alert.py` | 9 | SMTP / STARTTLS / SMTP_SSL selection, HTML body | Blocking `smtplib` inside `async def`. No `List-Unsubscribe`, no plain-text part, no double opt-in. |

## Not carried over (and why)

- SQLite store (`core/store/*`): the stack is PostgreSQL + SQLAlchemy.
- Web UI (`templates/`, Tailwind + Alpine.js): public pages must work without
  JavaScript under a strict CSP.
- Auth plugins (`auth/*`), Fernet credential encryption and the JSON schema extractor:
  authenticated checks were out of scope when this directory was created. ADR 0004 brought
  them into scope; they are rebuilt in Phase 7 (credential vault, auth methods, JSON
  response drift) under the current rules.
- SMS channel, `.NET` / Laravel middleware, plugin registry: not in the plan.
