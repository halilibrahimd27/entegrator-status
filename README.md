# Entegratör Status

Independent, public status page for Turkey's e-invoice ecosystem: GİB-authorised private
integrators (özel entegratörler) and GİB's own public services.

It answers two questions on one page:

1. **Is it up?** Availability and latency from at least two probe locations, with
   consensus, so one bad network never declares an outage.
2. **Did the contract change?** Structural drift in public WSDL/XSD contracts, plus
   upstream (GİB) announcements mapped to every provider that depends on them.

> Independent measurement, not official. / *Bağımsız ölçümdür, resmî değildir.*

## Status

Phase 0 (foundation) of the [build plan](docs/PLAN.md): configuration that fails closed,
the SSRF-guarded outbound HTTP client, CI and the container setup. No checks run yet.

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

- [CLAUDE.md](CLAUDE.md): ground rules (probing ethics, KVKK, security baseline, stack)
- [docs/PLAN.md](docs/PLAN.md): phased build plan
- [docs/METHODOLOGY.md](docs/METHODOLOGY.md): what we measure and how
- [docs/adr/](docs/adr/): architecture decisions
- [SECURITY.md](SECURITY.md): reporting vulnerabilities

---

## Türkçe

Türkiye e-Fatura ekosistemi için bağımsız durum sayfası: GİB'in yetkilendirdiği özel
entegratörler ve GİB'in herkese açık servisleri.

- **Çalışıyor mu?** En az iki ölçüm noktasından erişilebilirlik ve gecikme; tek bir
  ağdaki sorun "kesinti" ilan ettirmez.
- **Sözleşme değişti mi?** Herkese açık WSDL/XSD sözleşmelerindeki yapısal değişiklikler
  ve GİB duyurularının bağlı entegratörlere etkisi.

Ölçümler yalnızca herkese açık, kimlik doğrulaması gerektirmeyen adreslere yapılır;
istekler düşük frekanslıdır ve kendini tanıtır. Ayrıntılar: [Yöntem](docs/METHODOLOGY.md).

Hızlı başlangıç yukarıdaki komutlarla aynıdır: `make install`, `make env`, `make dev`.
