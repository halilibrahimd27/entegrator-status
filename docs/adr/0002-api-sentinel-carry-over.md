# ADR 0002 — Carry-over from api-sentinel

- Status: accepted
- Date: 2026-10-19
- Supersedes: the "clean-room" rule in the first draft of `CLAUDE.md`

## Context

The first draft of the plan required a clean-room rewrite: no code from `api-sentinel`.
The maintainer confirmed that `api-sentinel` is entirely their own work (not written for
an employer or client), so there is no ownership reason to rewrite it from scratch.

At the same time, most of api-sentinel conflicts with this project's rules: raw SQLite
instead of PostgreSQL, Alpine.js templates instead of no-JS pages, default secrets in
code, no SSRF guard, and authenticated probing that is out of scope. Copying it wholesale
would import exactly the problems the security baseline exists to prevent.

## Decision

- The useful parts are kept as **reference material** in `legacy/api-sentinel/`
  (inventory, target phase and known defects in its README). They are never imported,
  packaged, linted or tested.
- A legacy piece is ported only in the phase that needs it, rewritten into
  `src/sentinel/` under `CLAUDE.md`, and the legacy file is deleted in the same change.
- The `api-sentinel` repository is retired once the files are carried over.
- Code from any employer repository is still never copied.

## Consequences

- No blank-page start for alerts (Phase 7), page watchers (Phase 6) and contract
  severity (Phase 5), without lowering the bar for what lands in `src/`.
- `legacy/` shrinks phase by phase; when it is empty it is deleted.
