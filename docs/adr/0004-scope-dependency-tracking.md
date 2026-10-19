# ADR 0004 — Scope: dependency status and change tracking

- Status: accepted
- Date: 2026-10-19
- Supersedes: the e-invoice-only scope of the first plan draft

## Context

The first plan described a public status page for Turkey's e-invoice ecosystem (GİB and
private integrators). The maintainer's intent is broader: any team should be able to map
everything its product depends on and be warned when something upstream breaks or
changes. E-invoicing is one domain with a clear third-degree chain (product → integrator
→ GİB), not the product's boundary.

## Decision

- **Multi-tenant with a shared catalog.** Organizations build their own dependency
  graphs. Common services (cloud providers, payment APIs, integrators, GİB, popular
  packages) live once in a curated shared catalog, with their upstream edges, and are
  observed once for everybody. Orgs can add private services.
- **Private panel, optional public page.** An org's graph, results and alerts are
  private; an org can publish a status page for its own customers.
- **Authenticated checks with the org's own credentials** are in scope (envelope-
  encrypted, write-only, sent only to the configured host).
- **Change sources:** vendor status pages, API contract drift (OpenAPI, JSON responses,
  WSDL/XSD), announcements and changelogs, releases (GitHub, npm, PyPI, OCI), security
  advisories (OSV) and end-of-life dates (endoflife.date).
- **Extensibility through source adapters** with one typed interface and a shared
  contract test suite, so new kinds of dependencies do not touch the core.
- E-invoicing stays as the first curated catalog pack (`tr-efatura`), with the human
  curation and robots rules from ADR 0003.

## Consequences

- New phases for accounts/tenancy (1), imports (2), releases/advisories (6) and the
  credential vault (7). The plan grows from 8 to 11 phases.
- Tenant isolation becomes a security property with its own test harness.
- Phase 0 is unaffected: config, the SSRF-guarded client and the container setup are
  domain-neutral.
- More api-sentinel ideas become relevant (authenticated checks, JSON response drift).
  Its auth plugins and credential encryption were not carried over before this decision;
  they are rebuilt in Phase 7 under the current rules.
