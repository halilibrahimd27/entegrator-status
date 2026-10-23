# Entegratör Status

Status and change tracking for everything your product depends on.

Map your dependencies (third-party APIs, SaaS vendors, integrators, cloud services,
packages, container images, certificates, domains) and the shared catalog adds the
dependencies of those dependencies. Entegratör Status watches every node and tells you,
once and early, when something upstream breaks or changes in a way that can break you.

1. **Is it up?** Availability from at least two probe locations with consensus, next to
   what the vendor's status page claims.
2. **Did it change?** API contract drift (OpenAPI, JSON, WSDL/XSD), releases, advisories,
   end-of-life dates, announcements and deprecation notices, classified by impact.
3. **Does it affect me?** Impact follows the dependency graph up to the third degree:
   *your product → your vendor → your vendor's upstream*.

Example: GİB updates the UBL-TR code lists → your e-invoice integrator has to adapt →
your invoicing module is flagged before the effective date.

## Status

Phases 0–9 of the [build plan](docs/PLAN.md) are done:

- configuration that fails closed, the SSRF-guarded outbound HTTP client, CI, containers
- accounts: sign-up with e-mail verification, sign-in with lockout, password reset,
  server-side sessions, CSRF protection, TOTP (mandatory for platform admins)
- organizations, roles, invitations and read-only API tokens, isolated per tenant by
  the ORM session itself ([ADR 0005](docs/adr/0005-tenant-isolation.md))
- KVKK: data export, account deletion, closing organizations, retention jobs
- the dependency graph: products, a curated shared catalog (YAML packs, see
  [docs/CATALOG.md](docs/CATALOG.md)) plus private services, components and sources;
  third-degree view as server-rendered SVG; imports from SBOMs, lockfiles and compose
  files; proposals to the shared catalog with a platform-admin review queue
- the prober: `http`, `tls_cert`, `dns` and `domain_expiry` checks; robots.txt,
  one request per host at a time, `Retry-After` / `Crawl-delay` and backoff; results
  buffered locally (SQLite) and shipped in signed batches, at-least-once; node
  self-health from control targets; identical sources of different tenants probed once
- the status engine: consensus of at least two healthy nodes with hysteresis, a
  per-component timeline, automatic incidents with cause hints, maintenance windows,
  manual incidents for your own products, uptime (24 h to 90 d), 90-day bars, an
  organization status page and `GET /api/v1/status`
  ([ADR 0008](docs/adr/0008-status-engine.md))
- change sources: vendor status pages (Statuspage, Instatus, RSS) shown next to our
  measurements, RSS/Atom feeds and watched pages (CSS selectors), each fetched by one
  node with robots.txt every cycle; announcements classified in Turkish and English with
  effective dates; manual announcements; dated risks from `Sunset` / `Deprecation`
  headers and expiring certificates and domains
  ([ADR 0009](docs/adr/0009-change-sources.md))
- releases, advisories and lifecycle: GitHub, npm, PyPI and Docker Hub releases
  classified major / minor / patch / pre-release (semver, PEP 440, calver); pinned vs.
  latest on product pages; risks for unadopted majors, OSV advisories that affect the
  pinned version, and end of life within 180 days
  ([ADR 0010](docs/adr/0010-registries.md))
- API contract drift: OpenAPI, JSON response shapes and WSDL/XSD normalised to
  structural facts and classified breaking / non-breaking / cosmetic; authenticated
  checks with the organization's own credentials (write-only vault, sent only to their
  host, only through trusted probe nodes; [ADR 0011](docs/adr/0011-credentials.md))
- impact: every incident, change and risk on the products it reaches (≤ 3 hops) with
  the reason path and usage-aware severity; a risk register with per-organization
  decisions; a planned-changes calendar; correlated-failure hints; a weekly digest
- alerts: rules by product, service, kind and severity; e-mail (double opt-in, one-click
  unsubscribe), Telegram, Slack-compatible and signed webhooks
  ([docs/WEBHOOKS.md](docs/WEBHOOKS.md)); one message per event, channel and recipient;
  retries, auto-disable, quiet hours, a daily batch and the weekly digest by e-mail

Next: the panel dashboard and public status pages (Phase 10).

## Quick start

Requirements: Python 3.12, Docker with Compose, [gitleaks](https://github.com/gitleaks/gitleaks),
and [uv](https://github.com/astral-sh/uv) (only for `make lock`).

```bash
make install   # venv + hashed dev dependencies + pre-commit hook
make env       # .env with fresh random secrets (mode 600, gitignored)
make dev       # web, engine, postgres, redis, mailpit (+ prober once a node exists)
make node      # register a local probe node (credentials go to .env), start the prober
open http://127.0.0.1:8000      # sign up; mails land in http://127.0.0.1:8025
```

| Command | What it does |
|---|---|
| `make test` | pytest against throwaway PostgreSQL/Redis (no network) and the coverage gates (security, tenancy, accounts, sources, graph, catalog, probe, engine, risk) |
| `make lint` | ruff, ruff format check, mypy --strict |
| `make security` | bandit, pip-audit, gitleaks |
| `make check` | all of the above; must pass before every commit |
| `make lock` | regenerate hashed lock files (14-day release cool-down) |

## Layout

```
src/sentinel/      application package (config, security, web, probe, engine, …)
tests/             pytest suite
docs/              plan, methodology, ADRs
```

## Docs

- [CLAUDE.md](CLAUDE.md): ground rules (tenancy, credentials, probing ethics, KVKK, security, stack)
- [docs/PLAN.md](docs/PLAN.md): phased build plan
- [docs/METHODOLOGY.md](docs/METHODOLOGY.md): what we observe and how
- [docs/WEBHOOKS.md](docs/WEBHOOKS.md): receiving and verifying alert webhooks
- [docs/adr/](docs/adr/): architecture decisions
- [SECURITY.md](SECURITY.md): reporting vulnerabilities

---

## License

[Apache-2.0](LICENSE). Entegratör Status is self-hosted software: whoever runs an
instance operates it ([ADR 0012](docs/adr/0012-distribution.md)).

---

## Türkçe

Ürününün bağımlı olduğu her şey için durum ve değişiklik takibi.

Bağımlılıklarını tanımla (üçüncü parti API'ler, SaaS, entegratörler, bulut servisleri,
paketler, container image'ları, sertifikalar, domain'ler); ortak katalog onların
bağımlılıklarını ekler. Entegratör Status her düğümü izler ve yukarıda bir şey bozulduğunda
ya da seni bozacak şekilde değiştiğinde haber verir: bir kez, erken ve sınıflandırılmış
olarak.

- **Çalışıyor mu?** En az iki ölçüm noktasından erişilebilirlik; vendor'ın kendi durum
  sayfasıyla yan yana.
- **Değişti mi?** API sözleşme değişiklikleri, sürümler, güvenlik bildirimleri, destek
  sonu tarihleri, duyurular ve kullanımdan kaldırma notları.
- **Beni etkiler mi?** Etki bağımlılık grafında üç dereceye kadar izlenir:
  *ürünün → vendor'ın → vendor'ın bağımlılığı*.

Amaç, düzenli yapılan angaryayı (changelog okumak, durum sayfalarını takip etmek, bitiş
tarihlerini elle kontrol etmek) ortadan kaldırmak ve gözden kaçan riskleri (bir
bağımlılığın güncellemesiyle ürünün bozulması gibi) sıfırlamak.

Hızlı başlangıç yukarıdaki komutlarla aynıdır: `make install`, `make env`, `make dev`.
