# Methodology

> Stub. Completed phase by phase; the public `/yontem` page (Phase 4) is rendered from
> the final version of this document.

## What we measure

- **Availability and latency** of public, unauthenticated endpoints (portal pages,
  publicly documented WSDLs, announcement pages), from at least two probe locations.
- **Contract drift** of public WSDL/XSD documents (Phase 5).
- **Upstream announcements** (GİB and providers), mapped to dependent providers (Phase 6).

## What we do not measure

- Anything behind a login, or any service meant only for integrators.
- Invoice delivery end to end: we never send documents through anyone's system.

## Status rules

- `down` only when **≥ 2 healthy probe nodes** see failures for **3 consecutive rounds**;
  with one healthy node the state is `unverified`.
- Recovery after **3 consecutive successes**.
- `degraded`: p95 latency above the component threshold for ≥ 15 minutes.
- Nodes that fail their own control targets are excluded from the round.
- Uptime is computed from the consensus timeline; declared maintenance is excluded.

## Probe locations

_To be filled in Phase 8 (two nodes in different networks, preferably both in Turkey)._

## How our bot behaves

See [ADR 0003](adr/0003-probing-ethics.md). The public `/hakkinda/bot` page (Phase 4)
lists rate limits, the User-Agent and an opt-out contact.

## Corrections

Providers can request a correction through the public form (Phase 4). Every decision is
audit-logged.
