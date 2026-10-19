# ADR 0003 — Probing ethics

- Status: accepted (amended by ADR 0004 for authenticated checks)
- Date: 2026-10-19

## Context

We observe other organisations' services, usually without their involvement, and many
tenants may depend on the same vendor. Doing that carelessly would multiply load on
small providers, break their terms, or publish claims we cannot back up. Some sources
forbid automation outright: GİB's e-Belge site disallows it in robots.txt (observed
October 2026).

## Decision

Rules are enforced in code, not left to convention:

1. **Only what the org named.** Public endpoints, or endpoints the org is entitled to use
   with **its own** credentials (confirmed in the UI). Never anyone else's credentials;
   never scanning, fuzzing or brute force.
2. **Read-only.** `GET` / `HEAD`. The only exceptions are an OAuth2 token request and an
   explicitly flagged read query (e.g. GraphQL), each limited to the configured endpoint.
3. **Low and bounded load, across all tenants.** Availability ≥ 60 s per URL per node
   (default 120 s); contract, release, feed and page fetches ≥ 1 h (default 6 h); one
   concurrent request per host per node (`HostLimiter`); a per-host token budget shared by
   all tenants; back-off on `429` / `503` + `Retry-After`. Identical shared sources are
   observed once and fanned out. Per-org quotas cap sources and intervals.
4. **Use the front door.** Documented APIs (Statuspage, GitHub, npm, PyPI, OSV,
   endoflife.date, RDAP) with conditional requests and their published limits; no HTML
   scraping where an API exists.
5. **Identify ourselves.** `EntegratorStatus/<version> (+<site>/hakkinda/bot; <contact>)`.
6. **robots.txt.** Content watchers check it before every cycle and honour `Crawl-delay`;
   when disallowed they stop and say so ("robots.txt izin vermiyor") and announcements are
   entered by hand. Single-URL availability probes may ignore robots only behind a
   per-source flag set deliberately.
7. **Human-curated catalog.** Official lists (e.g. the integrator list on
   `ebelge.gib.gov.tr`) are read in a browser, never scraped. Every URL records its source.
8. **Factual presentation.** No logos; "Bağımsız ölçümdür, resmî değildir" wherever
   third-party measurements are public; measured wording ("Erişim sorunu tespit edildi
   (2/2 ölçüm noktası)"); a correction form whose outcomes are audit-logged.

## Consequences

Some services will only ever have a portal check or manual announcements. That is
acceptable: components may exist without sources and dependency edges still carry impact.
