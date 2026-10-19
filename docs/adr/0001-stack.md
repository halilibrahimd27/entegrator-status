# ADR 0001 — Stack

- Status: accepted
- Date: 2026-10-19

## Context

Entegratör Status is a small, security-sensitive public service: it fetches URLs that
admins and subscribers enter, serves public pages that must work without JavaScript, and
runs probe nodes on networks we do not control. The stack has to be boring, typed and
easy to audit.

## Decision

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12, `mypy --strict` | One language for web, prober and engine; strict typing catches config and model drift. |
| Web / API | FastAPI + Jinja2 (server-rendered), HTMX in admin only | No SPA: pages must render without JS and be CDN-cacheable. |
| DB | PostgreSQL 16, SQLAlchemy 2 (async), Alembic | Time-range queries on check results, real migrations. |
| Queue / cache | Redis 7 | Alert fan-out and rate limiting only. |
| HTTP client | httpx (async) behind `sentinel.security.http` | Async, typed, supports per-request SNI (`sni_hostname`), which IP pinning needs. |
| XML | lxml (hardened) + defusedxml | WSDL/XSD parsing without XXE or entity expansion. |
| Scheduling | asyncio inside the `prober` process | No Celery; per-host limits live in-process next to the requests. |
| Tests | pytest, pytest-asyncio, httpx `MockTransport` | No real network; `tests/conftest.py` blocks non-loopback sockets and DNS. |
| Quality | ruff, mypy, bandit, pip-audit, gitleaks | Run by `make check` locally, in pre-commit and in CI. |
| Packaging | Docker (non-root, read-only root FS), Compose, Caddy in production | Same image for all three services. |

Supply chain:

- Dependencies are locked with hashes (`uv pip compile --generate-hashes`) and installed
  with `pip --require-hashes --no-deps`. `make lock` never picks a release younger than
  14 days (`--exclude-newer`), so a freshly published malicious release cannot land on
  the next lock.
- Base images are pinned by digest; GitHub Actions by commit SHA.
- The image runs the source tree via `PYTHONPATH` instead of installing a wheel, so the
  build needs no unhashed build backend.

## Notes

- Starlette's `TestClient` now prefers `httpx2` (pydantic's successor to httpx). It is a
  **dev-only** dependency for tests. Moving the product's outbound client to `httpx2`
  needs its own ADR and a re-check of `sni_hostname` support.
- api-sentinel used APScheduler and raw SQLite; both are replaced (see ADR 0002).

## Consequences

Changing any row above requires a new ADR first.
