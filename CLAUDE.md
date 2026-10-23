# CLAUDE.md — Entegratör Status

Status and change tracking for everything a software product depends on. A team maps its
dependencies (third-party APIs, SaaS vendors, integrators, cloud services, packages,
container images, certificates, domains) and the shared catalog adds the dependencies
of those dependencies. Entegratör Status watches every node and tells the team, once and
early, when something upstream breaks or changes in a way that can break them.

Three questions, one panel:

1. **Is it up?** Availability and latency from at least two probe locations, with
   consensus, shown next to what the vendor's own status page claims.
2. **Did it change?** API contract drift (OpenAPI, JSON responses, WSDL/XSD), releases
   (GitHub, npm, PyPI, container tags), announcements and changelogs, deprecation and
   sunset notices — each classified by how much it matters.
3. **Does it affect me?** Incidents, changes and risks propagate along the dependency
   graph up to the **third degree** (my product → my vendor → my vendor's upstream), with
   the path that connects them to the product.

The point is to remove toil (reading changelogs, following status pages, checking expiry
dates by hand) and to catch the risks that slip through: a dependency update that breaks
production, an API version sunset announced only in a changelog, a certificate that
expires on a Sunday.

Uptime alone is a commodity (Uptime Kuma exists). The differentiators are the **shared
catalog** with curated upstream edges, the **dependency graph with third-degree impact**,
**consensus-based incidents** and **change classification** (breaking vs. non-breaking,
usage-aware). Protect those; don't re-invent generic monitoring features.

Read `docs/PLAN.md` before any non-trivial work. Work phase by phase; never start a phase
before the previous one meets its acceptance criteria.

## Ground rules (non-negotiable)

### Carry-over from api-sentinel
- The maintainer owns `api-sentinel`. The pieces worth keeping were rewritten into
  `src/sentinel/` under this file's rules, phase by phase; the last ones (alert channels)
  landed in Phase 9 and the `legacy/` directory is gone (ADR 0002).
- Never copy code from any employer repository.

### Tenancy and isolation
- Multi-tenant. An **Organization** has members with roles `owner | admin | member |
  viewer`. Platform admins (us) curate the shared catalog.
- Every tenant-owned row carries `org_id`. Repository functions require an explicit
  tenant scope and refuse to run without one. A two-tenant test harness proves that no
  route or repository function reads or writes across tenants (expected result: 404).
- Catalog services are `shared` (curated, observed once for everybody) or `private`
  (owned by one org, visible only to it). Promoting a private service to the shared
  catalog is a reviewed platform-admin action and never copies credentials, private
  results or usage data.
- One org's graph, credentials, results, alerts and subscribers are never visible to
  another org, including through aggregates on public pages.

### Credentials (authenticated checks)
- An org may attach **its own** credentials (API key header, bearer token, basic auth,
  OAuth2 client credentials) to sources of services it is entitled to use; the UI asks
  the user to confirm that. Never use anyone else's credentials.
- Envelope encryption: each secret is encrypted with AES-256-GCM under a per-org data
  key; data keys are encrypted with `CREDENTIALS_MASTER_KEY`. Secrets are **write-only**:
  never returned by the API, never rendered, never logged, never stored in snapshots.
- Credentials are sent only to the host they were configured for. A redirect that
  leaves that host drops them.
- Authenticated checks are read-only (`GET`/`HEAD`). The only non-idempotent requests
  allowed are an OAuth2 token request and an explicitly flagged read query (e.g.
  GraphQL), each limited to the configured endpoint.

### Good-citizen probing (ethics + legal)
- Observe public endpoints, or the org's own authorised endpoints as above. Never scan,
  fuzz, brute-force or probe anything the org did not name.
- Rate limits are enforced in code, globally per target host across all tenants:
  availability checks every ≥ 60 s per URL per node (default 120 s); contract, release,
  feed and page fetches every ≥ 1 h (default 6 h); at most **1 concurrent request per
  host per node** and a per-host request budget. Honour `429` / `503` + `Retry-After`
  with exponential backoff.
- Identical sources on shared services are **deduplicated**: observed once, fanned out
  to every org that depends on them. Per-org quotas cap the number of sources and the
  minimum interval.
- Use documented APIs (Statuspage JSON, GitHub, npm registry, PyPI JSON, OSV,
  endoflife.date, RDAP) with conditional requests (`ETag` / `If-Modified-Since`) and
  their published rate limits. Never scrape HTML when an API exists.
- Every request sends `User-Agent: EntegratorStatus/<version> (+https://<domain>/hakkinda/bot; <contact-email>)`.
- **robots.txt:** content watchers (fetching, parsing and storing page content) MUST
  check robots.txt with `urllib.robotparser` before every fetch cycle and honour
  `Crawl-delay`. If disallowed, the watcher is disabled with the visible reason
  "robots.txt izin vermiyor" and announcements for that source are entered by hand.
  Single-URL availability probes (one fixed URL, low frequency, nothing stored but
  status/latency) may run regardless, behind a per-source flag set consciously.
  - Example (Oct 2026): `ebelge.gib.gov.tr` disallows automated fetching. The `tr-efatura`
    catalog pack is curated by a human; the integrator list page is never scraped.
- Public pages: no logos or brand imagery, provider names used factually, every page
  that shows third-party measurements says "Bağımsız ölçümdür, resmî değildir" and links
  to the methodology. Wording is measured: "Erişim sorunu tespit edildi (2/2 ölçüm
  noktası)", never "X çöktü". Providers can request corrections; decisions are
  audit-logged.

### Personal data (KVKK)
- Personal data: user accounts (name, e-mail), memberships, alert recipients and
  subscribers of public status pages (e-mail, Telegram chat id, webhook URL).
- Aydınlatma metni at sign-up, data minimisation, double opt-in for e-mail subscribers,
  one-click unsubscribe in every message, documented retention (unconfirmed sign-ups
  48 h, unsubscribed contacts 30 days, closed orgs purged after 30 days), export and
  deletion on request.
- For subscribers of an org's public page the org is the data controller and we are the
  processor; the terms say so.

### Security baseline (lessons from earlier projects, enforced in code and tests)
- **Fail closed on config.** The app refuses to start if a required secret is missing,
  shorter than 32 bytes, a known placeholder or equal to another key. No secret has a
  default value in code, compose files or docs.
- **One key, one purpose.** Session signing, token hashing, webhook signing, ingest
  signing and credential encryption use separate keys.
- **SSRF:** every user-entered URL (sources, OAuth token URLs, spec URLs, webhooks) goes
  through one guard: `https` only (plain `http` only behind an explicit per-source flag),
  DNS resolved and the connection **pinned to the resolved IP**, private, loopback,
  link-local, CGNAT and metadata ranges blocked, at most 3 redirects with each hop
  re-validated, response size caps (2 MB pages, 5 MB contracts), strict timeouts.
- **Parsing:** XML only with XXE-safe settings (`defusedxml`, or `lxml` with
  `resolve_entities=False, no_network=True, huge_tree=False`); imported XSDs fetched
  through the guard, same host only, depth ≤ 3. JSON/YAML with size and depth limits,
  YAML via `safe_load` only. Uploaded files (SBOMs, lockfiles) are size-capped, parsed,
  never executed and not kept longer than the import.
- **Account auth:** argon2id, login rate limiting and lockout, server-side sessions with
  `Secure; HttpOnly; SameSite=Strict` cookies, CSRF tokens on every form, TOTP optional
  for members and mandatory for platform admins. No token ever travels in a URL query
  string. Org API tokens are scoped, expiring and stored hashed.
- **Probe nodes:** per-node token stored hashed; result batches are HMAC-signed with a
  timestamp and rejected outside a 5-minute window (replay protection).
- **Outbound webhooks:** HMAC-SHA256 signature over timestamp + body, retries with
  backoff, auto-disable after repeated failures, SSRF guard.
- **Templates:** Jinja2 autoescape on, strict CSP (no inline scripts), security headers.
- **Containers:** non-root, read-only root FS where possible, image digests pinned.
  PostgreSQL and Redis are never published on host ports; the web service binds
  `127.0.0.1` behind a reverse proxy.
- No secrets in the repo; `.env.example` holds placeholders only; gitleaks runs in
  pre-commit and CI. Logs never contain secrets, tokens, credentials or full webhook URLs.

## Stack (fixed — changing anything requires an ADR in `docs/adr/` first)

| Concern | Choice |
|---|---|
| Language | Python 3.12, fully typed |
| Web / API | FastAPI, Jinja2 server-rendered pages, HTMX for small interactions; no SPA |
| DB | PostgreSQL 16, SQLAlchemy 2 (async) + Alembic migrations |
| Queue / cache | Redis 7 (alert fan-out and rate limiting only) |
| HTTP client | httpx (async) behind our own SSRF-guarded transport |
| XML | lxml (hardened) + defusedxml |
| Crypto | `cryptography` (AES-256-GCM envelope encryption), `argon2-cffi` |
| Scheduling | asyncio scheduler inside the `prober` process; no Celery |
| Alerts | SMTP e-mail, Telegram Bot API, Slack-compatible and signed webhooks |
| Metrics | prometheus-client (`/metrics`, internal only) |
| Packaging | Docker + Docker Compose; Caddy as reverse proxy in production |
| Tests | pytest, pytest-asyncio, httpx `MockTransport` / pytest-httpserver; **no real network in tests** |
| Quality | ruff, mypy --strict, bandit, pip-audit, gitleaks |

Charts are server-rendered inline SVG (sparklines, uptime bars, graph views). Pages must
work without JavaScript; public pages must be cacheable by a CDN.

## Architecture

```
probe node(s) ──signed batches──▶ ingest API ─▶ PostgreSQL ◀─ engine ─▶ alerts (Redis queue)
   (prober: availability          (web)                       (consensus, change        e-mail / Telegram /
    + change sources)                                          classification, impact,   Slack / webhook
                                                               risk, rollups)
org panel + public status pages + JSON API (web, server-rendered) ◀── reads ──┘
```

- `prober`: runs every **source adapter** under rate limits and robots rules, buffers
  results and snapshots, ships signed batches to the ingest API. Runs locally or on a
  remote node.
- `web`: org panel, public status pages, JSON API, ingest API, platform admin.
- `engine`: consensus, incidents, change classification, impact propagation, risk
  register, uptime rollups, alert scheduling. Separate process; idempotent jobs.
- **Source adapters** share one typed interface: `fetch → normalise → snapshot → diff →
  classify`. Each kind (`http`, `status_page`, `release`, `api_contract`, …) is one
  adapter registered by name and covered by a shared contract test suite. Supporting a
  new kind of dependency means adding an adapter, not changing the core.

Package layout (src layout):
`sentinel/{config,db,models,accounts,tenancy,catalog,graph,sources,probe,engine,contracts,risk,alerts,web,admin,security}`.

### Domain vocabulary (use these names in code)
- **Organization**, **User**, **Membership** (role), **ApiToken**, **AuditLog**.
- **Product** — the org's own system; the root of its graph.
- **Service** — anything depended on: vendor API, SaaS, integrator, cloud service,
  package, container image, domain, internal system. `visibility: shared | private`.
- **Component** — a monitorable part of a service (portal, REST API v2, webhook
  delivery, region, package `react`…).
- **Source** — how a component is observed, by kind:
  availability `http`, `tls_cert`, `dns`, `domain_expiry`; vendor status `status_page`;
  change `api_contract`, `release`, `feed`, `page_watch`; risk `advisory`, `eol`.
- **Dependency** — directed edge (Product or Component) → Component, with optional
  `usage` (pinned version, endpoints/operations used) that makes impact usage-aware.
  Shared edges (e.g. every integrator's e-Fatura API → GİB central e-Fatura) are curated.
- **ProbeNode**, **Observation** (one check result), **Incident** (+ updates),
  **MaintenanceWindow**.
- **Snapshot** — normalised state of a change source (contract model, release list,
  feed items) + hash.
- **Change** — classified difference between two snapshots:
  contracts `breaking | non_breaking | cosmetic`; releases `major | minor | patch |
  prerelease`; announcements `maintenance | deprecation | security | package_update |
  incident | other`.
- **Risk** — a dated, open condition: certificate or domain expiring, end of life,
  sunset/deprecation date, known vulnerability in a used version, unadopted major.
- **Impact** — an incident, change or risk propagated to dependants (depth ≤ 3) with its
  reason path.
- **Credential** — an encrypted secret attached to an org's sources.
- **AlertRule**, **Channel**, **Subscriber** (public status page).

### Status semantics
- A component is `down` only when **≥ 2 healthy nodes** see failure for **N consecutive
  rounds** (default 3). With a single healthy node the state is `unverified`, never `down`.
- Recovery needs **M consecutive successes** (default 3) — hysteresis against flapping.
- `degraded`: p95 latency above the component threshold for ≥ 15 minutes.
- A node that fails its own control targets is marked unhealthy and its results are
  excluded (our network problem must not become a provider outage).
- Uptime % (24 h / 7 d / 30 d / 90 d) is computed from the consensus timeline, never
  from raw node results. Declared maintenance is shown separately and excluded.
- Vendor-declared status (from `status_page` sources) is shown next to ours, never
  merged into our consensus.

### Change semantics
- A change is reported once per snapshot transition. Each adapter's normaliser removes
  noise (timestamps, ordering, whitespace, rotating tokens) so content churn is not a
  change.
- Impact severity = change class × usage match × depth. A removed operation that the
  product's `usage` lists is `breaking` for that product; without usage data it is
  "possibly affected", worded as such.
- Hints (e.g. "ortak bir kaynaktan kaynaklanıyor olabilir") are always worded as hints,
  never as assertions.

## Commands

```bash
make dev        # docker compose up (web, prober, engine, postgres, redis) with hot reload
make test       # pytest (unit + integration, no network) + coverage gates
make lint       # ruff check, ruff format --check, mypy --strict
make security   # bandit, pip-audit, gitleaks
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
  module outside `sentinel.security` imports an HTTP client library.

## Definition of done (per phase)
1. Acceptance criteria in `docs/PLAN.md` all met.
2. `make check` passes; coverage ≥ 85 % for `security`, `accounts`, `tenancy`, `graph`,
   `sources`, `contracts`, `engine` and `risk` once they exist.
3. Docs updated (README, `docs/METHODOLOGY.md`, ADRs for decisions taken).
4. End with a **phase report**: what was done, how to test it by hand, known gaps.
