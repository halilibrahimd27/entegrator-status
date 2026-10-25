# Changelog

All notable changes. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/). Upgrade steps for each
version are in [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md#upgrading).

## [0.1.0] - 2026-10-25

The first release: everything in Phases 0–11 of [docs/PLAN.md](docs/PLAN.md).

### Added

- Accounts with e-mail verification, sessions, CSRF protection, TOTP (mandatory for
  platform admins); organizations, roles, invitations, read-only API tokens; tenant
  isolation enforced by the ORM session.
- The dependency graph: products, a curated shared catalog (YAML packs), private
  services, three-degree views, imports from SBOMs, lockfiles and compose files.
- Probe nodes with an SSRF-guarded HTTP client, robots.txt, per-host rate limits,
  signed at-least-once batches; consensus status from at least two nodes, incidents,
  maintenance windows, uptime and 90-day bars.
- Change sources: vendor status pages, feeds, watched pages, releases (GitHub, npm,
  PyPI, Docker Hub), OSV advisories, end-of-life dates, API contract drift (OpenAPI,
  JSON, WSDL/XSD), authenticated checks with a write-only credential vault.
- Impact up to three hops with usage-aware severity, a risk register, a calendar,
  correlated-failure hints and a weekly digest.
- Alerts by e-mail, Telegram, Slack-compatible and signed webhooks, with rules, quiet
  hours, daily batches, retries and auto-disable ([docs/WEBHOOKS.md](docs/WEBHOOKS.md)).
- The dashboard, a read-only JSON API, optional public status pages with Atom, badge
  and subscriptions, a static export for GitHub Pages
  ([docs/STATIC_EXPORT.md](docs/STATIC_EXPORT.md)), methodology, bot and correction pages.
- Self-hosting: `compose.prod.yml` with Caddy, Redis ACL, per-organization source
  quotas, platform administration, `/metrics`, backups and key rotation.

[0.1.0]: https://github.com/halilibrahimd27/entegrator-status/releases/tag/v0.1.0
