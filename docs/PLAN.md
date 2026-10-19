# Entegratör Status — Build Plan

Read `CLAUDE.md` first; its ground rules override anything here.
One phase per session. Finish a phase completely (acceptance criteria + `make check` +
phase report) before starting the next. If a phase is too big for one session, split it
into slices (`2a`, `2b`), each leaving `main` green.

---

## Why this exists (context for decisions)

- Turkish e-invoicing runs through GİB-authorised private integrators. When an
  integrator or GİB has a problem, accounting firms, ERP vendors and e-commerce sellers
  find out from angry customers. There is no independent place to check "is it them or
  me?" and no early warning when the rules change.
- The rules change often and upstream of everyone. Examples:
  - **Dec 2024:** GİB's planned cutover (14 Dec 2024) of e-Fatura/e-İrsaliye to a new
    central application came with a new WSDL and SOAP 1.2 only (1.0/1.1 dropped).
    Every integrator had to adapt.
  - **12 Jan 2026:** GİB announced a UBL-TR 1.2.1 package update (new XML field for
    investment-incentive invoices).
  - **14 Sep 2026:** updated e-Fatura / e-Arşiv packages and UBL-TR code lists went live.
- These upstream changes are third-degree dependencies for end users (their software →
  their integrator → GİB). Making them visible, per provider, is the core of this product.
- Integrators rarely publish test endpoints openly (some hand them out via support), so
  many providers will only have a public portal check. That's fine: the catalog must
  allow components without probes.

---

## Phase 0 — Foundation

Goal: an empty but production-shaped repo where security rules are already enforced.

Tasks
- [ ] src-layout Python package `sentinel`, `pyproject.toml`, pinned dependencies with hashes.
- [ ] `sentinel/config.py`: typed settings (pydantic-settings). Fail closed: missing,
      short (< 32 bytes) or placeholder secrets abort startup with a clear message.
      Separate keys: `SESSION_KEY`, `TOKEN_HASH_KEY`, `WEBHOOK_SIGNING_KEY`, `INGEST_HMAC_KEY`.
- [ ] `sentinel/security/http.py`: the only outbound HTTP client.
  - SSRF guard as described in CLAUDE.md (https only by default, DNS resolve + IP
    pinning, blocked ranges incl. 100.64.0.0/10, 169.254.0.0/16, ::1, fc00::/7, fe80::/10,
    redirects ≤ 3 re-validated, size caps, timeouts).
  - Per-host concurrency limiter and token-bucket rate limiter (in-process, testable).
  - Standard User-Agent.
- [ ] A test that fails if any module outside `sentinel/security` imports `httpx`.
- [ ] Docker: multi-stage `Dockerfile` (non-root), `compose.yml` for dev (web, prober,
      engine, postgres, redis; DB/Redis **not** published on host ports).
- [ ] `Makefile` targets from CLAUDE.md. Pre-commit: ruff, ruff-format, mypy, gitleaks.
- [ ] GitHub Actions `ci.yml`: lint, type check, tests, bandit, pip-audit, gitleaks.
      Every action pinned by commit SHA; `permissions: contents: read`.
- [ ] `docs/adr/0001-stack.md`, `0002-api-sentinel-carry-over.md`, `0003-probing-ethics.md`.
- [ ] `README.md` skeleton (English first, Turkish section), `docs/METHODOLOGY.md` stub,
      `sentinel/web/i18n/tr.yaml`.

Acceptance
- `make check` green locally and in CI.
- Tests prove: startup aborts on a missing/placeholder/short secret; SSRF guard blocks
  each private range, a DNS-rebinding attempt (resolve public → connect private), a
  redirect to a private address and an oversized response.
- `docker compose up` brings all services up; `GET /healthz` returns 200.

---

## Phase 1 — Catalog and data model

Goal: providers, components, checks and dependencies exist, curated by a human.

Tasks
- [ ] SQLAlchemy models + Alembic migration for: Provider, Component, Check, Dependency,
      ProbeNode, CheckResult, Incident, IncidentUpdate, MaintenanceWindow,
      ContractSnapshot, Announcement, Subscriber, Subscription, AdminUser, AuditLog,
      CorrectionRequest. Indices for time-range queries on CheckResult.
- [ ] Catalog file `catalog/providers.yaml` (+ JSON Schema) and CLI:
      `sentinel catalog validate`, `sentinel catalog import` (idempotent upsert, dry-run
      by default, prints a diff).
- [ ] Every check URL is validated through the SSRF guard at import time.
- [ ] Flags per check: `availability_ignores_robots` (default false), `allow_http`
      (default false), `interval_s`, `timeout_s`, `expected_status`, optional
      `body_must_contain`, `latency_degraded_ms`.
- [ ] Ship **only example entries** using `*.example.invalid` hosts, plus a GİB authority
      provider with components but **no URLs filled in**. Do not invent real endpoints.
- [ ] `docs/CATALOG.md`: the human curation workflow:
  1. Take provider names from GİB's official list
     (`https://ebelge.gib.gov.tr/efaturaozelentegratorlerlistesi.html`), opened by hand
     in a browser — the site disallows automated access, never scrape it.
  2. For each provider, add only endpoints that are **public without credentials**,
     verified by hand (portal home/login page, publicly documented WSDL, public
     announcement or blog page).
  3. Endpoints meant for integrators only are out of scope. Treat GİB's central
     e-Fatura service that way unless it is verified to be public and intended for
     public use. Model such services as components with no checks so dependencies
     still work.
  4. Record the source of every URL in the catalog entry (`source:` field).
- [ ] Default dependency edges: every integrator component tagged `efatura_service`
      depends on the GİB central e-Fatura component.

Acceptance
- `sentinel catalog validate` rejects: duplicate slugs, non-https URLs without
  `allow_http`, private IPs, intervals below the minimums, unknown dependency targets.
- Import is idempotent (running twice changes nothing) and audit-logged.

---

## Phase 2 — Prober

Goal: polite, correct measurements shipped safely to the core.

Tasks
- [ ] Check runners: `http` (status, latency, optional body assertion, TLS info) and
      `tls_cert` (days to expiry, issuer, chain validity).
- [ ] Scheduler with jitter; respects per-check interval, per-host concurrency = 1,
      per-host budget, `Retry-After`/backoff.
- [ ] robots.txt module: cached per host (≤ 24 h), `Crawl-delay` honoured, used by
      `page_watch` (Phase 6) and by `http` checks unless `availability_ignores_robots`.
- [ ] Error classification: `dns`, `connect_timeout`, `read_timeout`, `tls`,
      `http_4xx`, `http_5xx`, `assertion`, `blocked_by_robots`, `rate_limited_by_us`.
- [ ] Node self-health: each node checks 3 control targets (configurable, stable public
      sites); failing ≥ 2 marks the node unhealthy in its batch.
- [ ] Ingest: results buffered locally (SQLite or file queue) and shipped in batches to
      `POST /ingest/v1/results` with HMAC signature + timestamp; at-least-once delivery,
      idempotent on the server (result UUIDs).
- [ ] `sentinel node register <name> <location>` prints a one-time token (stored hashed).

Acceptance
- Tests with local fake servers cover every error class, Retry-After, the rate limiter
  (never two concurrent requests to one host), robots disallow and crawl-delay, replay
  rejection (old timestamp, reused batch id, bad signature) and buffering while ingest
  is down.

---

## Phase 3 — Status engine

Goal: trustworthy state, incidents and uptime.

Tasks
- [ ] Consensus per component per round (see "Status semantics" in CLAUDE.md):
      `operational | degraded | down | unverified | maintenance`.
- [ ] Incident lifecycle: auto-open after N failing rounds with ≥ 2 healthy nodes,
      auto-resolve after M successes, merge flaps within 10 minutes, root-cause hint
      from error classes ("TLS sertifika hatası", "zaman aşımı", "HTTP 5xx").
- [ ] Maintenance windows (manual or from announcements) suppress incidents and are
      excluded from uptime.
- [ ] Rollups: minute state buckets → hourly latency p50/p95 → daily uptime; uptime
      24 h / 7 d / 30 d / 90 d from consensus only.
- [ ] Retention jobs: raw results 30 days, minute buckets 120 days, daily forever.

Acceptance
- Table-driven tests: 1 node failing → `unverified`; 2 nodes failing 3 rounds → `down`;
  alternating results → no incident spam; unhealthy node excluded; maintenance excluded
  from uptime; uptime math verified on hand-computed timelines (including DST-free UTC
  day boundaries rendered in Europe/Istanbul).

---

## Phase 4 — Public site

Goal: fast, honest, no-JS status pages.

Pages (Turkish copy from `tr.yaml`)
- [ ] `/` — all providers: current state, 90-day uptime bar, last incident; filter by
      state; GİB shown first as the upstream.
- [ ] `/e/{slug}` — provider: components with state, uptime 24h/7d/30d/90d, latency
      sparkline, open and past incidents, contract changes, announcements, **"Bağlı
      olduğu servisler"** (dependencies) and current upstream impact banner.
- [ ] `/olay/{id}` — incident timeline.
- [ ] `/yontem` — methodology: probe locations, intervals, consensus rule, what we
      do *not* measure, robots policy, how to request a correction.
- [ ] `/hakkinda/bot` — what our bot does, rate limits, contact, opt-out for providers.
- [ ] `/duzeltme` — correction request form (CSRF, rate limited, honeypot; no third-party
      captcha or trackers).
- [ ] JSON API: `/api/v1/status`, `/api/v1/providers/{slug}`, `/api/v1/incidents`.
- [ ] Atom feeds: global and per provider. Embeddable SVG badge `/badge/{slug}.svg`.
- [ ] SEO: titles/descriptions per provider, sitemap.xml, canonical URLs.
- [ ] Caching: `Cache-Control` + `ETag`; pages render in < 150 ms from warm cache.
- [ ] Footer on every page: "Bağımsız ölçümdür, resmî değildir" + methodology link.

Acceptance
- All pages render with JavaScript disabled. CSP has no `unsafe-inline`.
- Template tests: every dynamic value escaped; provider names with `<script>` render
  inert.
- Accessibility: semantic HTML, labelled form fields, sufficient contrast. Run axe-core
  or Lighthouse by hand on `/` and a provider page; record the scores in the phase report.

---

## Phase 5 — Contract drift (WSDL / XSD)

Goal: detect when a public contract changes and say how much it matters.

Tasks
- [ ] `wsdl_contract` check: fetch WSDL (+ imported XSDs, same host, depth ≤ 3) through
      the SSRF guard with the hardened XML parser.
- [ ] Normaliser → structural model: services, ports/addresses, bindings with **SOAP
      version**, operations, input/output messages, element and complex types with
      cardinality and type. Ignore whitespace, ordering and documentation.
- [ ] Classifier:
  - `breaking`: operation removed/renamed, required element added or removed, type
    changed, cardinality narrowed, binding SOAP version changed, endpoint address changed.
  - `non_breaking`: optional element added, new operation, enum value added.
  - `cosmetic`: documentation/annotation only.
- [ ] Store `ContractSnapshot` (normalised model + hash + diff); public change log per
      provider with a readable diff.
- [ ] Fixtures (synthetic, written for tests): SOAP 1.1 → 1.2 binding change, removed
      operation, added optional element, doc-only change, XXE payload, billion-laughs,
      oversized WSDL, import pointing to another host.

Acceptance
- Every fixture classified correctly; XXE/billion-laughs/oversized/cross-host import are
  rejected without crashing the check.

---

## Phase 6 — Upstream watchers and impact (the third-degree layer)

Goal: when the upstream changes, every affected provider page shows it — before users
find out the hard way.

Tasks
- [ ] `page_watch` check: fetch an announcement page (robots-checked every cycle),
      extract items with a configurable CSS selector (title, date, link), store only
      title + date + link (never copy article bodies), detect new items.
- [ ] Classifier for announcements (Turkish keywords + rules): `maintenance`
      (bakım, kesinti, erişim), `package_update` (UBL-TR, şematron, paket, kod listesi,
      XSLT), `test_env` (test ortamı), `regulation` (tebliğ, VUK). Unit-tested keyword
      tables.
- [ ] Manual announcement form in admin for sources whose robots.txt disallows us
      (e.g. GİB e-Belge). Fields: provider, title, link, published date, effective date,
      kind, affected components.
- [ ] Impact mapping: an `authority` announcement or incident propagates along
      Dependency edges and shows a "Yukarı akış etkisi / olası etki" banner on every
      dependent provider page, plus a planned-changes calendar on `/` (e.g.
      "14.09.2026 — UBL-TR kod listeleri güncellemesi").
- [ ] Correlated-failure hint: if ≥ 40 % (configurable) of integrators' `efatura_service`
      components go `down` within the same 10-minute window, show "Ortak bir kaynaktan
      kaynaklanıyor olabilir" on `/`. It is a hint, worded as such, never an assertion.

Acceptance
- Tests: robots disallow disables the watcher and surfaces the reason; selector change
  yields zero items → watcher flagged `needs_attention` instead of silently "no news";
  impact propagates two hops (authority → integrator component → provider page);
  correlated-failure hint fires at the threshold and not below.

---

## Phase 7 — Subscriptions and alerts

Goal: people get told, once, on the channel they chose.

Tasks
- [ ] Subscription scopes: provider, component, "GİB değişiklikleri" (authority
      announcements + impact), everything. Event types: incident open/resolve, degraded,
      contract change (`breaking` only by default), announcement.
- [ ] E-mail: double opt-in, one-click unsubscribe (`List-Unsubscribe` header +
      link), plain-text + HTML templates.
- [ ] Telegram: bot with deep-link subscription (`/start <token>`), `/abonelikler`,
      `/iptal`. Webhook mode in production, long polling in dev.
- [ ] Webhooks: JSON payload, `X-Sentinel-Signature` (HMAC-SHA256 over body + timestamp),
      retries with backoff, auto-disable after repeated failures, SSRF guard on target.
- [ ] Fan-out through Redis; dedup + cooldown per subscriber/event; optional daily digest.
- [ ] KVKK: aydınlatma metni page, explicit consent checkbox, retention job deletes
      unconfirmed sign-ups after 48 h and unsubscribed contacts after 30 days.
- [ ] Rate limits on subscribe endpoints; honeypot field.

Acceptance
- Tests: one incident → exactly one message per matching subscriber per channel;
  unconfirmed e-mail receives nothing; unsubscribe link works without login and is
  single-use-safe; webhook signature verifiable with a reference snippet in the docs;
  private-IP webhook rejected.

---

## Phase 8 — Admin and production

Goal: run it for real, safely, with two probe nodes.

Tasks
- [ ] Admin panel (server-rendered + HTMX): catalog CRUD (SSRF-validated), incidents
      (manual updates, public notes), maintenance windows, announcements, correction
      requests, subscriber counts (no bulk export), audit log viewer. Optional TOTP.
- [ ] `compose.prod.yml` + Caddy (automatic HTTPS, security headers, cache for public
      pages), `/metrics` reachable only on the internal network.
- [ ] Backups: nightly `pg_dump` to off-site storage + a documented, tested restore drill.
- [ ] Remote probe node runbook (`docs/PROBE_NODE.md`): minimal container, outbound-only,
      token provisioning, clock sync requirement (HMAC window).
- [ ] `docs/LAUNCH.md` checklist: two healthy nodes in different networks (prefer both
      in Turkey — some Turkish public services restrict foreign IPs; verify), catalog
      reviewed, methodology published, contact e-mail live, KVKK texts live.
- [ ] README (EN + TR): what it is, screenshots, architecture diagram, methodology link,
      self-hosting guide, and a **"How it was built"** section: designed and reviewed by
      the maintainer, implemented with Claude Code.

Acceptance
- Fresh VM: follow README → stack up with HTTPS in < 30 minutes.
- Restore drill restores yesterday's backup into a clean database and the site renders.
- Security review pass: no secret defaults, no host-published DB/Redis ports, CSP
  strict, all forms CSRF-protected, all outbound fetches through the guard.

---

## Backlog (not in the MVP — don't build, don't scaffold)
- Paid tier (more subscriptions, webhooks, history export) — likely iyzico.
- Opt-in authenticated synthetic checks using a customer's own test credentials.
- e-Arşiv / e-İrsaliye / e-Defter specific components and contracts.
- English locale for public pages.
- Integrator comparison reports (factual uptime only; needs legal review first).
