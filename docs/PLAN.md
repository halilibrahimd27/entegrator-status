# Entegratör Status — Build Plan

Read `CLAUDE.md` first; its ground rules override anything here.
One phase per session. Finish a phase completely (acceptance criteria + `make check` +
phase report) before starting the next. If a phase is too big for one session, split it
into slices (`2a`, `2b`), each leaving `main` green.

---

## Why this exists (context for decisions)

- Every product runs on other people's systems: payment APIs, e-invoice integrators,
  shipping and marketplace APIs, cloud services, open-source packages, container images.
  They change upstream, often quietly, and the team finds out from a production error
  or an angry customer.
- The cause is often two or three hops away. The vendor is fine; its upstream changed.
  Turkish e-invoicing is a clean example (end user → accounting software → private
  integrator → GİB):
  - **Dec 2024:** GİB's cutover of e-Fatura/e-İrsaliye to a new central application came
    with a new WSDL and SOAP 1.2 only. Every integrator had to adapt.
  - **12 Jan 2026:** UBL-TR 1.2.1 package update (new XML field for investment-incentive
    invoices).
  - **14 Sep 2026:** updated e-Fatura / e-Arşiv packages and UBL-TR code lists went live.
- Other domains have the same shape: a payment provider changing its 3-D Secure flow, a
  cloud region incident behind a SaaS, a library major release with breaking changes, an
  API version sunset announced only in a changelog, a certificate expiring.
- Teams handle this with toil (reading changelogs, following status pages, checking
  expiry dates, watching release feeds) and things still slip through.
- The product: map dependencies once (imports make it fast, the shared catalog fills in
  the upstream), then get told once, early and classified when something upstream breaks
  or changes, with the path that connects it to your product.
- Many vendors publish no test endpoints and no machine-readable status. That is fine:
  components may exist without sources so the graph and impact still work, and
  announcements can be entered by hand.

---

## Phase 0 — Foundation ✅

Goal: an empty but production-shaped repo where security rules are already enforced.

Tasks
- [x] src-layout Python package `sentinel`, `pyproject.toml`, pinned dependencies with hashes.
- [x] `sentinel/config.py`: typed settings (pydantic-settings). Fail closed: missing,
      short (< 32 bytes) or placeholder secrets abort startup with a clear message.
      Separate keys: `SESSION_KEY`, `TOKEN_HASH_KEY`, `WEBHOOK_SIGNING_KEY`, `INGEST_HMAC_KEY`.
- [x] `sentinel/security/http.py`: the only outbound HTTP client.
  - SSRF guard as described in CLAUDE.md (https only by default, DNS resolve + IP
    pinning, blocked ranges incl. 100.64.0.0/10, 169.254.0.0/16, ::1, fc00::/7, fe80::/10,
    redirects ≤ 3 re-validated, size caps, timeouts).
  - Per-host concurrency limiter and token-bucket rate limiter (in-process, testable).
  - Standard User-Agent.
- [x] A test that fails if any module outside `sentinel/security` imports `httpx`.
- [x] Docker: multi-stage `Dockerfile` (non-root), `compose.yml` for dev (web, prober,
      engine, postgres, redis; DB/Redis **not** published on host ports).
- [x] `Makefile` targets from CLAUDE.md. Pre-commit: ruff, ruff-format, mypy, gitleaks.
- [x] GitHub Actions `ci.yml`: lint, type check, tests, bandit, pip-audit, gitleaks.
      Every action pinned by commit SHA; `permissions: contents: read`.
- [x] `docs/adr/0001-stack.md`, `0002-api-sentinel-carry-over.md`, `0003-probing-ethics.md`.
- [x] `README.md` skeleton (English first, Turkish section), `docs/METHODOLOGY.md` stub,
      `sentinel/web/i18n/tr.yaml`.

Acceptance
- `make check` green locally and in CI.
- Tests prove: startup aborts on a missing/placeholder/short secret; SSRF guard blocks
  each private range, a DNS-rebinding attempt (resolve public → connect private), a
  redirect to a private address and an oversized response.
- `docker compose up` brings all services up; `GET /healthz` returns 200.

---

## Phase 1 — Accounts and tenancy ✅

Goal: people sign up, form organisations, and can trust that their data stays theirs.

Tasks
- [x] SQLAlchemy models + first Alembic migration: Organization, User, Membership
      (`owner | admin | member | viewer`), Invitation, ApiToken, AuditLog; platform-admin
      flag on User.
- [x] Sign-up with e-mail verification, login (argon2id), rate limiting and lockout,
      password reset (single-use, expiring token; the link opens a form that POSTs the
      token, page sends `Referrer-Policy: no-referrer`).
- [x] Server-side sessions (`Secure; HttpOnly; SameSite=Strict`), CSRF on every form,
      session rotation on login, TOTP (optional for members, mandatory for platform
      admins).
- [x] Tenancy layer `sentinel/tenancy`: a `TenantScope` that every repository function
      requires; query helpers that refuse to run unscoped. Decide in an ADR whether
      PostgreSQL row-level security is added as a second layer.
- [x] Permission matrix in one module (role × action), used by every route.
- [x] Org API tokens: scoped, expiring, shown once, stored hashed with `TOKEN_HASH_KEY`.
- [x] Audit log: auth events, membership and role changes, token and credential operations.
- [x] KVKK: aydınlatma metni at sign-up, data export and account / org deletion
      (org purge after 30 days).
- [x] Minimal server-rendered pages (Turkish copy from `tr.yaml`): sign-up, login, org
      switcher, members, tokens.

Acceptance
- Two-tenant harness: every route and repository function returns 404 for another
  org's objects; unscoped queries raise.
- Auth tests: lockout, reset token single-use and expiring, CSRF required on every POST,
  session fixation prevented, TOTP enforced for platform admins, viewer cannot write.
- No secret, token or password hash ever appears in logs (canary test).

---

## Phase 2 — Catalog and dependency graph ✅

Goal: an org describes what it depends on in minutes; the shared catalog fills in the
upstream.

Tasks
- [x] Models + migration: Product, Service (`visibility: shared | private`, `kind`),
      Component, Source (`kind`, per-adapter config validated by JSON Schema, `interval_s`,
      `timeout_s`, flags `allow_http`, `availability_ignores_robots`), Dependency
      (Product|Component → Component, optional `usage`: pinned version, operations used).
- [x] Shared catalog as YAML packs `catalog/packs/*.yaml` (+ JSON Schema) and CLI:
      `sentinel catalog validate`, `sentinel catalog import` (idempotent upsert, dry-run
      by default, prints a diff, audit-logged). Every URL validated through the SSRF guard
      at import time, every URL records its `source:`.
- [x] Ship **only example entries** using `*.example.invalid` hosts, plus a `tr-efatura`
      pack skeleton: GİB as authority with components and **no URLs filled in**, and the
      edge template "integrator `efatura_service` → GİB central e-Fatura". Do not invent
      real endpoints. `docs/CATALOG.md` documents the human curation workflow (official
      lists opened by hand in a browser, never scraped; public endpoints only; source of
      every URL recorded; integrator-only services modelled as components without sources).
- [x] Graph rules: cycles rejected on write, impact depth ≤ 3, shared edges editable only
      by platform admins; orgs add their own edges from their products and private services.
- [x] Imports that fill a graph fast (preview → confirm): CycloneDX JSON and SPDX JSON
      SBOMs, `package-lock.json`, `requirements.txt` / `poetry.lock`, container images from
      a `compose.yml`, an OpenAPI spec URL (→ API component + contract source). Imported
      packages map to catalog entries by purl.
- [x] Propose a private service for the shared catalog; review queue for platform admins.
- [x] Panel pages: products, services, add from catalog, dependency view (server-rendered
      SVG, depth ≤ 3).

Acceptance
- `sentinel catalog validate` rejects: duplicate slugs, non-https URLs without
  `allow_http`, private IPs, intervals below the minimums, unknown dependency targets,
  cycles.
- Import is idempotent (running twice changes nothing) and audit-logged.
- SBOM/lockfile fixtures produce the expected nodes; oversized, deeply nested and YAML-bomb
  uploads are rejected without crashing.
- Private services and edges are invisible to other orgs (two-tenant harness).

---

## Phase 3 — Prober (availability) ✅

Goal: polite, correct availability measurements shipped safely to the core.

Tasks
- [x] Source adapter interface (`fetch → normalise → snapshot → diff → classify`) and
      registry; shared contract tests every adapter must pass.
- [x] Availability adapters: `http` (status, latency, optional body assertion, TLS info,
      `Deprecation` / `Sunset` headers recorded), `tls_cert` (days to expiry, issuer,
      chain validity), `dns`, `domain_expiry` (RDAP).
- [x] Scheduler with jitter; per-source interval, per-host concurrency = 1, global per-host
      budget across tenants, `Retry-After` / backoff. Shared sources scheduled once and
      fanned out.
- [x] robots.txt module: cached per host (≤ 24 h), `Crawl-delay` honoured.
- [x] Error classification: `dns`, `connect_timeout`, `read_timeout`, `tls`, `http_4xx`,
      `http_5xx`, `assertion`, `blocked_by_robots`, `rate_limited_by_us`.
- [x] Node self-health: 3 configurable control targets; failing ≥ 2 marks the node
      unhealthy for the batch.
- [x] Ingest: local buffer (SQLite or file queue), batches to `POST /ingest/v1/results`
      with HMAC signature + timestamp, at-least-once delivery, idempotent on the server.
- [x] `sentinel node register <name> <location>` prints a one-time token (stored hashed).

Acceptance
- Local fake servers cover every error class, Retry-After, the rate limiter (never two
  concurrent requests to one host), robots disallow and crawl-delay, replay rejection
  (old timestamp, reused batch id, bad signature) and buffering while ingest is down.
- Two orgs depending on the same shared source → exactly one probe per interval.

Notes
- Exponential backoff on `429` and on `503` + `Retry-After`; a plain `5xx` keeps the
  normal interval so a recovery is seen on time. `Retry-After` and `Crawl-delay` hold
  the whole host.
- Results whose host stays held longer than their interval are reported as
  `rate_limited_by_us` (unknown, not down).
- Results leave the node buffer only after the core accepted them; a refused result is
  isolated and dropped so it cannot block the queue; `401/403` keeps everything.

---

## Phase 4 — Status engine ✅

Goal: trustworthy state, incidents and uptime.

Tasks
- [x] Consensus per component per round: `operational | degraded | down | unverified |
      maintenance` (CLAUDE.md "Status semantics"; [ADR 0008](adr/0008-status-engine.md)).
- [x] Incident lifecycle: auto-open after N failing rounds with ≥ 2 healthy nodes,
      auto-resolve after M successes, merge flaps within 10 minutes, root-cause hint from
      error classes ("TLS sertifika hatası", "zaman aşımı", "HTTP 5xx").
- [x] Maintenance windows (manual or from announcements) suppress incidents and are
      excluded from uptime. Orgs may add manual incidents for their own products.
      (Organizations declare windows for their own components, before they start;
      windows on shared components are platform-level and arrive with announcements
      in Phase 5, with an admin form in Phase 10.)
- [x] Rollups: minute buckets → hourly latency p50/p95 → daily uptime; uptime
      24 h / 7 d / 30 d / 90 d from consensus only.
- [x] Retention: raw observations 30 days, minute buckets 120 days, daily forever.

Notes
- Uptime windows are rolling and computed exactly from the timeline; daily rows feed
  the 90-day bars. `degraded` and `unverified` count as up; maintenance and time
  without data are left out of the ratio.
- Org status page (`/o/{slug}/durum`) and `GET /api/v1/status` list every component
  the products depend on (≤ 3 hops), worst first.

Acceptance
- Table-driven tests: 1 node failing → `unverified`; 2 nodes failing 3 rounds → `down`;
  alternating results → no incident spam; unhealthy node excluded; maintenance excluded
  from uptime; uptime verified on hand-computed timelines (UTC days rendered in
  Europe/Istanbul).

---

## Phase 5 — Change sources: status pages, feeds, announcements ✅

Goal: vendor statements and announcements arrive classified, without anyone reading them.

Tasks
- [x] `status_page` adapter: Atlassian Statuspage (`/api/v2/summary.json`), Instatus,
      generic RSS/Atom incident feeds; map vendor components to ours; vendor status shown
      next to our consensus, never merged. (Vendor maintenance becomes a maintenance
      window only when announced before it starts; otherwise it stays an announcement.)
- [x] `feed` adapter: RSS/Atom with `ETag` / `If-Modified-Since`
      ([ADR 0009](adr/0009-change-sources.md): one node per change source, snapshots).
- [x] `page_watch` adapter: robots-checked every cycle, items extracted with a
      configurable CSS selector (title, date, link only — never article bodies), new items
      detected (port `legacy/api-sentinel/watchers`).
- [x] Announcement classifier with Turkish + English keyword tables: `maintenance`
      (bakım, kesinti, erişim, maintenance), `deprecation` (sunset, deprecated, kullanımdan
      kaldırılacak), `security`, `package_update` (UBL-TR, şematron, paket, kod listesi,
      XSLT, SDK), `incident`, `other`. Effective dates extracted when present.
- [x] `Deprecation` / `Sunset` headers seen by `http` sources become Risks with dates.
      (Expiring certificates and domains become Risks too; the register with states
      arrives in Phase 8.)
- [x] Manual announcement form for sources that disallow automation (fields: service,
      title, link, published date, effective date, kind, affected components).

Acceptance
- Adapter contract tests pass for every new adapter.
- robots disallow disables the watcher and shows the reason; a selector change that yields
  zero items flags the watcher `needs_attention` instead of silently "no news".
- A Statuspage fixture maps components and incidents correctly; a `Sunset` header yields a
  dated Risk; keyword tables are unit-tested in both languages.

---

## Phase 6 — Releases, advisories and lifecycle ✅

Goal: "a dependency update broke production" stops being a surprise.

Tasks
- [x] `release` adapters: GitHub releases/tags (optional platform `GITHUB_TOKEN` for rate
      limits), npm registry, PyPI JSON, OCI / Docker Hub tags; conditional requests.
      ([ADR 0010](adr/0010-registries.md): PyPI through its RSS feed because robots.txt
      disallows the JSON API; `GITHUB_TOKEN` waits for Phase 7.)
- [x] Version classification: `major | minor | patch | prerelease` (semver), calver and
      non-semver tags handled explicitly; changelog / release-notes link extracted; release
      notes scanned for "breaking" markers.
- [x] Pinned vs. latest for imported dependencies (from `usage.version`): drift shown,
      unadopted majors become Risks with the release notes link.
- [x] `advisory` adapter: OSV.dev for package@version → security Risks with severity.
      (One query per package; the engine checks every pinned version against the
      affected ranges, so each organization gets risks for the versions it uses.)
- [x] `eol` adapter: endoflife.date for runtimes, frameworks and databases → Risk N days
      before end of life. (N = 180; ended cycles at once.)

Acceptance
- Registry fixtures for each ecosystem; semver edge cases (pre-releases, build metadata,
  calver, `v`-prefixed tags); `304 Not Modified` creates no new snapshot; an OSV fixture
  produces a Risk only for affected versions.

---

## Phase 7 — API contract drift and authenticated checks ✅

Goal: detect when an API a product uses changes shape, and say how much it matters.

Tasks
- [x] Credential vault: AES-256-GCM envelope encryption with per-org data keys and
      `CREDENTIALS_MASTER_KEY` (new fail-closed secret); write-only API; rotation; audit.
      ([ADR 0011](adr/0011-credentials.md); `make env` adds missing keys to an existing
      `.env`.)
- [x] Auth methods for sources: API key in a header, bearer, basic, OAuth2 client
      credentials (token URL through the SSRF guard; POST allowed only there). Credentials
      dropped when a redirect leaves the configured host. API keys in query strings are
      refused. (Authenticated jobs reach only nodes registered with `--credentials`,
      sealed for that node.)
- [x] `api_contract` adapters, each with a normaliser to a structural model:
  - OpenAPI 3.x spec URL (paths, operations, parameters, request/response schemas).
  - JSON response shape inferred from sample responses (port `legacy/api-sentinel/contracts`).
  - WSDL/XSD: services, ports/addresses, bindings with SOAP version, operations,
    messages, element and complex types with cardinality (hardened parser, imports same
    host, depth ≤ 3).
- [x] Classifier: `breaking` (operation/path removed or renamed, required field or
      parameter added/removed, type changed, cardinality narrowed, enum value removed,
      SOAP version or endpoint address changed), `non_breaking` (optional field added, new
      operation, enum value added), `cosmetic` (documentation only). Usage-aware: severity
      per product from `usage.operations`.
- [x] Snapshots store the normalised model + hash + readable diff; never response bodies
      with personal data or secrets.
- [x] Synthetic fixtures: SOAP 1.1 → 1.2, removed operation, added optional element,
      doc-only change, OpenAPI removed path / new required parameter / enum removal, XXE,
      billion laughs, oversized document, cross-host import.

Notes
- Contracts are flat "facts" (`sentinel.contracts`): operations, parameters, fields with
  type / required / nullable / enum / cardinality, servers and ports, security schemes,
  and a hash of documentation (cosmetic). Never examples, descriptions or bodies.
- A contract that is too large is refused, never truncated (a cut contract would read
  as removed operations). Path template names are normalised (`{id}` = `{userId}`).
- Usage-aware severity per product (matching `usage.operations` against the
  operations each change lists) lands with impact propagation in Phase 8.

Acceptance
- Every fixture classified correctly; XXE, billion laughs, oversized and cross-host
  imports rejected without crashing the source.
- A canary credential never appears in logs, API responses, error messages, snapshots or
  diffs; a cross-host redirect strips credentials; credentials are tenant-isolated.

---

## Phase 8 — Impact and risk

Goal: every incident, change and risk lands on the products it affects, with the reason.

Tasks
- [ ] Propagation along dependency edges up to depth 3 with a reason path, e.g.
      "Fatura modülü → Entegratör X e-Fatura API → GİB merkezi e-Fatura: UBL-TR kod
      listesi güncellemesi (14.09.2026)".
- [ ] Severity model: change class × usage match × depth attenuation.
- [ ] Risk register per org: open risks ordered by due date (certificate/domain expiry,
      end of life, sunset, advisories, unadopted majors); states `open | snoozed |
      accepted | resolved`, audit-logged.
- [ ] Planned-changes calendar from effective dates (announcements, sunset headers, EOL).
- [ ] Correlated-failure hint: if ≥ 40 % (configurable) of the dependants of one upstream
      go `down` within the same 10-minute window, show "Ortak bir kaynaktan kaynaklanıyor
      olabilir" — a hint, never an assertion.
- [ ] Weekly digest per org: what changed upstream, what is due.

Acceptance
- Two- and three-hop propagation tests; usage-aware severity (used operation removed →
  breaking, unused → possibly affected); risk lifecycle transitions; the hint fires at the
  threshold and not below.

---

## Phase 9 — Alerts

Goal: the right people get told once, on the channel they chose.

Tasks
- [ ] Channels per org: e-mail (double opt-in for external recipients, `List-Unsubscribe`),
      Telegram (deep-link `/start <token>`, `/abonelikler`, `/iptal`), Slack-compatible
      webhooks, signed webhooks (`X-Sentinel-Signature`: HMAC-SHA256 over timestamp + body,
      retries, auto-disable, SSRF guard) — port and fix `legacy/api-sentinel/alerts`.
- [ ] Alert rules: by product, service, kind (incident, change, risk), minimum severity;
      `breaking` changes only by default.
- [ ] Fan-out through Redis; dedup + cooldown per recipient/event; quiet hours; daily digest.
- [ ] KVKK retention jobs (unconfirmed 48 h, unsubscribed 30 days).

Acceptance
- One event → exactly one message per matching recipient per channel; unconfirmed
  recipients get nothing; unsubscribe works without login and is single-use-safe; webhook
  signature verifiable with the reference snippet in the docs; private-IP webhook rejected;
  rules cannot route one org's events to another org.

---

## Phase 10 — Panel and public status pages

Goal: one screen that answers "is anything upstream about to hurt us?", plus an optional
public page for an org's own customers.

Tasks
- [ ] Org dashboard: products with derived state, open incidents, recent changes, due
      risks, upcoming planned changes; filters; HTMX only as enhancement.
- [ ] Optional public status page per org (`/s/{org}`): chosen products and components,
      incidents, subscribe (double opt-in), Atom feed, SVG badge, disclaimer and
      methodology link for third-party measurements.
- [ ] JSON API v1 with org API tokens (read scopes first).
- [ ] Bot and methodology pages: `/yontem`, `/hakkinda/bot` (what the bot does, rate
      limits, contact, opt-out for providers), `/duzeltme` correction form (CSRF, rate
      limited, honeypot; no third-party captcha or trackers).
- [ ] SEO for public pages, `Cache-Control` + `ETag`, render < 150 ms from warm cache.

Acceptance
- All pages render with JavaScript disabled. CSP has no `unsafe-inline`.
- Template tests: every dynamic value escaped; names containing `<script>` render inert.
- Accessibility: semantic HTML, labelled form fields, sufficient contrast; axe-core or
  Lighthouse scores for the dashboard and a public page recorded in the phase report.

---

## Phase 11 — Release (self-hosted, [ADR 0012](adr/0012-distribution.md))

Goal: anyone can run their own instance safely; the project site lives on GitHub Pages.

Tasks
- [ ] Platform admin: shared catalog CRUD (SSRF-validated), promotion queue, per-org
      quotas, incidents and public notes, correction requests, audit log viewer.
- [ ] `compose.prod.yml` + Caddy (automatic HTTPS, security headers, cache for public
      pages), Redis with ACL, `/metrics` only on the internal network.
- [ ] Release workflow: versioned images on GitHub Container Registry (digest-pinned
      base, SBOM), changelog, upgrade notes (migrations run by the `migrate` service).
- [ ] Backups: nightly `pg_dump` script and a tested restore drill;
      `CREDENTIALS_MASTER_KEY` backup and rotation drill (`rewrap`).
- [ ] Probe node runbook (`docs/PROBE_NODE.md`): minimal container, outbound-only,
      token provisioning, `--credentials` only for nodes you trust, clock sync (HMAC window).
- [ ] Static export of an organization's public status page to a GitHub Pages
      repository it owns (no public server needed).
- [ ] Project site on GitHub Pages from `docs/`: what it is, screenshots, self-hosting
      guide, methodology, bot page, KVKK text templates for operators.
- [ ] README (EN + TR): what it is, screenshots, architecture diagram, methodology link,
      self-hosting guide, and a **"How it was built"** section: designed and reviewed by
      the maintainer, implemented with Claude Code.
- [ ] Going public: history and placeholder audit, `legacy/` removed, then the
      repository is made public.

Acceptance
- Fresh VM: follow README → stack up with HTTPS in < 30 minutes.
- Restore drill restores yesterday's backup into a clean database and the site renders;
  credentials decrypt after restore.
- Security review pass: no secret defaults, no host-published DB/Redis ports, CSP strict,
  all forms CSRF-protected, all outbound fetches through the guard, tenant harness green.

---

## Backlog (not in the MVP — don't build, don't scaffold)
- Paid tiers (more sources, shorter intervals, longer history) — likely iyzico.
- Sign-in with Google / GitHub (OIDC).
- More ecosystems: Maven, Go modules, NuGet, Composer, RubyGems, Helm charts,
  Terraform providers.
- Reading lockfiles straight from a repository (GitHub App) instead of uploads.
- Multi-step synthetic flows.
- English locale for the UI.
- Provider comparison reports (factual uptime only; needs legal review first).
