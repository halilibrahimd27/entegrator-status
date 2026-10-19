# Methodology

> Stub. Completed phase by phase; the public `/yontem` page (Phase 10) is rendered from
> the final version of this document.

## What we observe

Only what an organisation adds to its dependency graph, plus the curated shared catalog:

- **Availability and latency** of endpoints, from at least two probe locations
  (Phase 3–4). Vendor-declared status from their status pages is shown next to ours,
  never merged into it (Phase 5).
- **Changes:** announcements and changelogs (Phase 5), releases, security advisories and
  end-of-life dates (Phase 6), API contract drift for OpenAPI, JSON responses and
  WSDL/XSD (Phase 7).
- **Risks with a date:** expiring certificates and domains, sunset and deprecation
  dates, end of life, known vulnerabilities in used versions (Phases 3, 5–8).

## What we do not do

- Probe anything an organisation did not name; scan, fuzz or brute-force.
- Write to anyone's system: probes are read-only (OAuth2 token requests and explicitly
  flagged read queries are the only exceptions).
- Store response bodies: snapshots keep normalised structure (contracts, release lists,
  announcement titles, dates and links), not content with personal data.
- Send an organisation's credentials anywhere but the host they were configured for.

## Status rules

- `down` only when **≥ 2 healthy probe nodes** see failures for **3 consecutive rounds**;
  with one healthy node the state is `unverified`.
- Recovery after **3 consecutive successes**.
- `degraded`: p95 latency above the component threshold for ≥ 15 minutes.
- Nodes that fail their own control targets are excluded from the round.
- Uptime is computed from the consensus timeline; declared maintenance is excluded.

## Change and impact rules

- A change is reported once per snapshot transition; noise (timestamps, ordering,
  rotating tokens) is normalised away.
- Contract changes are `breaking`, `non_breaking` or `cosmetic`; releases `major`,
  `minor`, `patch` or `prerelease`; announcements `maintenance`, `deprecation`,
  `security`, `package_update`, `incident` or `other`.
- Impact follows dependency edges up to three hops. When the product's usage is known
  (pinned version, operations used) severity is exact; otherwise the product is shown as
  "possibly affected".

## Probe locations

_To be filled in Phase 11 (two nodes in different networks, preferably both in Turkey)._

## How our bot behaves

See [ADR 0003](adr/0003-probing-ethics.md). The public `/hakkinda/bot` page lists rate
limits, the User-Agent and an opt-out contact.

## Corrections

Providers can request a correction through the public form. Every decision is
audit-logged.
