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

Phase 0 (foundation) of the [build plan](docs/PLAN.md) is done: configuration that fails
closed, the SSRF-guarded outbound HTTP client, CI and the container setup. Next: accounts
and tenancy (Phase 1).

## Quick start

Requirements: Python 3.12, Docker with Compose, [gitleaks](https://github.com/gitleaks/gitleaks),
and [uv](https://github.com/astral-sh/uv) (only for `make lock`).

```bash
make install   # venv + hashed dev dependencies + pre-commit hook
make env       # .env with fresh random secrets (mode 600, gitignored)
make dev       # web, prober, engine, postgres, redis
curl -s http://127.0.0.1:8000/healthz
```

| Command | What it does |
|---|---|
| `make test` | pytest (no network) and the coverage gate for `sentinel.security` |
| `make lint` | ruff, ruff format check, mypy --strict |
| `make security` | bandit, pip-audit, gitleaks |
| `make check` | all of the above; must pass before every commit |
| `make lock` | regenerate hashed lock files (14-day release cool-down) |

## Layout

```
src/sentinel/      application package (config, security, web, probe, engine, …)
tests/             pytest suite
docs/              plan, methodology, ADRs
legacy/            reference code from api-sentinel, ported phase by phase (ADR 0002)
```

## Docs

- [CLAUDE.md](CLAUDE.md): ground rules (tenancy, credentials, probing ethics, KVKK, security, stack)
- [docs/PLAN.md](docs/PLAN.md): phased build plan
- [docs/METHODOLOGY.md](docs/METHODOLOGY.md): what we observe and how
- [docs/adr/](docs/adr/): architecture decisions
- [SECURITY.md](SECURITY.md): reporting vulnerabilities

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
