# ADR 0003 — Probing ethics

- Status: accepted
- Date: 2026-10-19

## Context

We measure other organisations' services without their involvement. Doing that
carelessly would put load on small providers, break their terms, or publish claims we
cannot back up. GİB's e-Belge site disallows automated access in robots.txt (observed
October 2026).

## Decision

Rules are enforced in code, not left to convention:

1. **Public, unauthenticated endpoints only.** Never anyone else's credentials.
2. **`GET` / `HEAD` only** for probes (`FetchPolicy.method` cannot express anything else).
3. **Low and bounded load.** Availability checks every ≥ 60 s per URL per node (default
   120 s), contract fetches every ≥ 1 h (default 6 h), at most one concurrent request
   per host per node (`HostLimiter`), a per-host token budget, and back-off on
   `429` / `503` + `Retry-After`.
4. **Identify ourselves.** Every request sends
   `EntegratorStatus/<version> (+<site>/hakkinda/bot; <contact>)`.
5. **robots.txt.** Content watchers check it before every cycle and honour
   `Crawl-delay`; when disallowed they stop and say so ("robots.txt izin vermiyor"), and
   an admin enters announcements by hand. Single-URL availability probes may ignore
   robots only behind a per-check flag an admin sets deliberately.
6. **The catalog is curated by a human.** The official integrator list on
   `ebelge.gib.gov.tr` is read in a browser, never scraped. Every URL records its source.
7. **Factual presentation.** No logos; "Bağımsız ölçümdür, resmî değildir" on every page;
   measured wording ("Erişim sorunu tespit edildi (2/2 ölçüm noktası)"); a public
   correction form whose outcomes are audit-logged.

## Consequences

Some providers will only ever have a portal check. That is acceptable: the catalog
allows components without probes, and dependency edges still carry upstream impact.
