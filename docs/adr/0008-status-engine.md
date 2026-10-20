# ADR 0008 — Status engine: rounds, hysteresis, timelines

- Status: accepted
- Date: 2026-10-20

## Context

CLAUDE.md "Status semantics" fixes the rules (≥ 2 healthy nodes for `down`, N failing
rounds, M successes to recover, `degraded` after 15 slow minutes, maintenance excluded,
uptime from consensus only). This ADR records how they are computed.

## Decision

**Rounds.** A round is one interval of a source, aligned to the epoch. A round is final
`GRACE` (90 s) after it ends; later results are stored but do not rewrite history.
Each healthy node counts once per round, by its latest result within 1.5 intervals
before the round ends. Nodes jitter their schedule by ±10 %, so a node may have no
result inside one particular round; without the lookback a real outage seen by two
nodes would often look like "one node failing" and never become `down`. Latency
percentiles use only results inside the round.

**What counts.** Only `http`, `dns` and `tls_cert` are availability. `domain_expiry` is
a dated risk (alerts, Phase 10). Results that say nothing about the provider
(`blocked_by_robots`, `rate_limited_by_us`, `blocked_url`, `blocked_address`,
`response_too_large`, `unsupported`, our own `error`) and results of unhealthy nodes
are left out of the round.

**Verdicts and states.** `fail` (≥ 2 nodes fail), `suspect` (exactly one), `ok`,
`unknown`. Per source a small state machine (`sentinel.engine.consensus`):

- 3 consecutive `fail` rounds → `down`, dated back to the first of them; `unknown`
  rounds in between do not break the streak, a `suspect` round does.
- `down` ends only after 3 consecutive `ok` rounds, dated back to the first of them.
  A data gap never ends `down`.
- `suspect` or a first `fail` → `unverified`; the next `ok` → `operational`.
- `degraded`: round p95 above the http source's `latency_degraded_ms` for 15 minutes,
  dated back to when it started.
- 3 `unknown` rounds → `unknown` (shown as "no data").

A component is as bad as its worst availability source; `unknown` sources are ignored.

**Timeline.** `component_states` holds intervals (one open per component, enforced by
a partial unique index). History is never rewritten: a change dated before the open
interval started is clamped to that start. With one availability source per component
(the usual case) back-dated changes land exactly; with several, a back-dated change can
land up to one round late. We accept that over rewriting closed intervals.

**Incidents.** Opened when a component becomes `down`, resolved when it leaves `down`.
A new outage within 10 minutes of the last one reopens that incident instead of
opening another. The cause hint is the most common error class of the failing rounds.
No incident opens while a maintenance window of the component's owner is active:
platform windows for shared components, the organization's own windows for its own
components. An organization cannot silence a shared component for everyone.

**Engine-only settings.** `latency_degraded_ms` lives in the http source config but is
not part of the probe job identity, so organizations with different thresholds still
share one probe.

**Catch-up.** After downtime the engine judges at most 24 hours of rounds per source;
older rounds are skipped and the machines keep their state.

## Consequences

- Every rule is a pure function with table-driven tests (`tests/test_consensus.py`);
  the database part is tested on hand-written timelines (`tests/test_status_engine.py`).
- Changing N, M or the lookback changes history only for new rounds.
