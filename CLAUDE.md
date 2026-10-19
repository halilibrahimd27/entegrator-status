# CLAUDE.md — Entegratör Status

Independent, public status page for Turkey's e-invoice ecosystem: GİB-authorised private
integrators (özel entegratörler) and GİB's own public services. Two questions, one page:

1. **Is it up?** Availability and latency from at least two probe locations, with
   consensus, so one bad network never declares an outage.
2. **Did the contract change?** Structural drift in public WSDL/XSD contracts, plus
   upstream (GİB) announcements mapped to every provider that depends on them.

Uptime alone is a commodity (anyone can run Uptime Kuma). The differentiators are the
curated provider catalog, consensus-based incidents, WSDL contract drift and the
dependency-aware impact view ("GİB changed the UBL-TR package → these integrators are
affected"). Protect those; don't spend effort re-inventing generic monitoring features.

Read `docs/PLAN.md` before any non-trivial work. Work phase by phase; never start a phase
before the previous one meets its acceptance criteria.

## Ground rules (non-negotiable)

### Clean-room
- This is a fresh codebase. Do **not** copy code from `api-sentinel` or any employer
  repository. Ideas described in this file and `docs/PLAN.md` are the only carry-over.

### Good-citizen probing (ethics + legal)
- Probe **only public, unauthenticated** endpoints. Never use anyone else's credentials.
  Authenticated synthetic checks are out of scope for the MVP.
- Probes use `GET` or `HEAD` only. Never `POST` to a provider's API.
- Rate limits are enforced in code, not by convention: availability checks every
  ≥ 60 s per URL per node (default 120 s), contract fetches every ≥ 1 h (default 6 h),
  at most **1 concurrent request per host per node**, and a per-host request budget.
  Honour `429` / `503` + `Retry-After` with exponential backoff.
- Every request sends `User-Agent: EntegratorStatus/<version> (+https://<domain>/hakkinda/bot; <contact-email>)`.
- **robots.txt:** content watchers (fetching, parsing and storing page content) MUST
  check robots.txt with `urllib.robotparser` before every fetch cycle and honour
  `Crawl-delay`. If disallowed, the watcher is disabled with the visible reason
  "robots.txt izin vermiyor" and announcements for that source are entered manually by
  an admin. Single-URL availability probes (one fixed URL, low frequency, nothing stored
  but status/latency) may run regardless, but stay behind a per-check flag that the admin
  must set consciously.
  - Observed in Oct 2026: `ebelge.gib.gov.tr`'s robots rules disallow automated
    fetching. Do not scrape it, including the official integrator list page. The
    catalog is curated by a human (see PLAN Phase 1).
- No logos or brand imagery. Provider names are used factually. Every public page shows
  "Bağımsız ölçümdür, resmî değildir" and links to the methodology page.
- Wording is factual and measured: "Erişim sorunu tespit edildi (2/2 ölçüm noktası)",
  never "X çöktü".
- Providers can request corrections via a public form. Corrections are audit-logged.

### Personal data (KVKK)
- The only personal data is subscriber contact data (e-mail, Telegram chat id, webhook URL).
- Explicit consent with an aydınlatma metni, double opt-in for e-mail, one-click
  unsubscribe in every message, data minimisation, a documented retention period.

### Security baseline (lessons from earlier projects, enforced in code and tests)
- **Fail closed on config.** The app refuses to start if a required secret is missing,
  shorter than 32 bytes, or equal to a known placeholder. No secret has a default value
  in code, compose files or docs.
- **One key, one purpose.** Session signing, token hashing and webhook signing use
  separate keys.
- **SSRF:** every outbound URL (admin-entered checks, subscriber webhooks) goes through
  one guard: `https` only (plain `http` only behind an explicit per-check flag), DNS
  resolved and the connection **pinned to the resolved IP** (no DNS rebinding), private,
  loopback, link-local, CGNAT and metadata ranges blocked, at most 3 redirects with each
  hop re-validated, response size caps (2 MB pages, 5 MB WSDL/XSD), strict timeouts.
- **XML:** parse with XXE-safe settings only (`defusedxml`, or `lxml` with
  `resolve_entities=False, no_network=True, huge_tree=False`). Imported XSDs are fetched
  through the SSRF guard, same host only, depth ≤ 3.
- **Admin auth:** argon2id hashes, login rate limiting, session cookies
  `Secure; HttpOnly; SameSite=Strict`, CSRF tokens on every form, optional TOTP. No
  token ever travels in a URL query string.
- **Probe nodes:** per-node token stored hashed; result batches are HMAC-signed with a
  timestamp and rejected outside a 5-minute window (replay protection).
- **Outbound webhooks:** HMAC-SHA256 signature header, retries with backoff, SSRF guard.
- **Templates:** Jinja2 autoescape on, strict CSP (no inline scripts), security headers.
- **Containers:** non-root, read-only root FS where possible, image digests pinned.
  PostgreSQL and Redis are never published on host ports; the web service binds
  `127.0.0.1` behind a reverse proxy.
- No secrets in the repo; `.env.example` holds placeholders only; gitleaks runs in
  pre-commit and CI. Logs never contain secrets, tokens or full webhook URLs.

## Stack (fixed — changing anything requires an ADR in `docs/adr/` first)

| Concern | Choice |
|---|---|
| Language | Python 3.12, fully typed |
| Web / API | FastAPI, Jinja2 server-rendered pages, HTMX for small admin interactions; no SPA |
| DB | PostgreSQL 16, SQLAlchemy 2 (async) + Alembic migrations |
| Queue / cache | Redis 7 (alert fan-out and rate limiting only) |
| HTTP client | httpx (async) behind our own SSRF-guarded transport |
| XML | lxml (hardened) + defusedxml |
| Scheduling | asyncio scheduler inside the `prober` process; no Celery |
| Alerts | SMTP e-mail, Telegram Bot API, signed webhooks |
| Metrics | prometheus-client (`/metrics`, internal only) |
| Packaging | Docker + Docker Compose; Caddy as reverse proxy in production |
| Tests | pytest, pytest-asyncio, respx / pytest-httpserver; **no real network in tests** |
| Quality | ruff, mypy --strict, bandit, pip-audit, gitleaks |

Charts on public pages are server-rendered inline SVG (sparklines, uptime bars). Pages
must work fully without JavaScript and be cacheable by a CDN.

## Architecture

```
probe node(s) ──signed batches──▶ ingest API ─▶ PostgreSQL ◀─ status engine ─▶ alerts (Redis queue)
   (prober)                         (web)                       (incidents,         e-mail / Telegram / webhook
                                                                 uptime, drift)
public pages + admin (web, server-rendered)  ◀── reads ──┘
```

- `prober`: runs checks with rate limits and robots rules, buffers results, ships
  signed batches to the ingest API. Runs locally (with the stack) or on a remote node.
- `web`: public pages, JSON API, ingest API, admin panel.
- `engine`: consensus, incidents, uptime rollups, contract diffs, impact mapping, alert
  scheduling. Separate process; idempotent jobs.

Package layout (src layout): `sentinel/{config,db,models,probe,engine,contracts,watchers,alerts,web,admin,security}`.

### Domain vocabulary (use these names in code)
- **Provider** — an integrator or an authority (GİB). `kind: integrator | authority`.
- **Component** — a monitorable part of a provider (web portal, e-Fatura web service,
  announcement page…).
- **Check** — how a component is measured: `http`, `tls_cert`, `wsdl_contract`, `page_watch`.
- **ProbeNode**, **CheckResult**, **Incident** (+ updates), **MaintenanceWindow**.
- **ContractSnapshot** — normalised WSDL/XSD model + hash + diff vs. previous.
- **Announcement** — maintenance or change notice (watched or entered manually).
- **Dependency** — directed edge component → component it depends on (e.g. every
  integrator's e-Fatura service → GİB central e-Fatura service). Drives impact mapping.
- **Subscriber** / **Subscription** — who is notified about what.

### Status semantics
- A component is `down` only when **≥ 2 healthy nodes** see failure for **N consecutive
  rounds** (default 3). With a single healthy node the state is `unverified`, never `down`.
- Recovery needs **M consecutive successes** (default 3) — hysteresis against flapping.
- `degraded`: p95 latency above the component threshold for ≥ 15 minutes.
- A node that fails its own control targets is marked unhealthy and its results are
  excluded (our network problem must not become a provider outage).
- Uptime % (24 h / 7 d / 30 d / 90 d) is computed from the consensus timeline, never
  from raw node results. Declared maintenance is shown separately and excluded.

## Commands

```bash
make dev        # docker compose up (web, prober, engine, postgres, redis) with hot reload
make test       # pytest -q (unit + integration, no network)
make lint       # ruff check, ruff format --check, mypy --strict sentinel
make security   # bandit -q -r sentinel, pip-audit, gitleaks detect
make migrate    # alembic upgrade head
make check      # lint + security + test — must pass before every commit
```

## Conventions
- Code, identifiers, comments, commit messages, ADRs: **English**.
  User-facing UI copy: **Turkish** (kept in `sentinel/web/i18n/tr.yaml`, no hard-coded
  strings in templates) so an English locale can be added later.
- Conventional Commits. Keep Claude Code's default `Co-Authored-By` trailer; never
  disable it.
- No file over ~400 lines without a reason written at the top of the file. Split by
  responsibility, not by convenience.
- Tests assert behaviour, not source text. Every bug fix starts with a failing test.
- Time is always UTC in storage, `Europe/Istanbul` at the presentation edge.
- Every outbound fetch goes through `sentinel.security.http` — a test enforces that no
  module imports `httpx` directly.

## Definition of done (per phase)
1. Acceptance criteria in `docs/PLAN.md` all met.
2. `make check` passes; coverage ≥ 85 % for `engine`, `contracts`, `security`.
3. Docs updated (README, `docs/METHODOLOGY.md`, ADRs for decisions taken).
4. End with a **phase report**: what was done, how to test it by hand, known gaps.
