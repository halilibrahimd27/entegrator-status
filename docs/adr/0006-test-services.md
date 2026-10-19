# ADR 0006 — Real PostgreSQL and Redis in tests

- Status: accepted
- Date: 2026-10-19

## Context

The tenant guard, migrations, constraints and JSONB behaviour are PostgreSQL-specific;
an in-memory substitute would test a different database. Tests must still never touch
the real network.

## Decision

- `make test` starts `compose.test.yml`: PostgreSQL 16 and Redis 7 with data on tmpfs,
  ports bound to `127.0.0.1` only (55432, 56379), trust authentication. CI runs the same
  images as job-local service containers.
- This is the one place where database ports are published on the host. It is allowed
  because the services hold no data, accept only loopback connections and exist only
  while tests run. The application stack (`compose.yml`) keeps them unpublished.
- The test network guard (`tests/conftest.py`) still blocks every non-loopback
  connection and DNS lookup.
- The schema is built once per run by the real migrations; tables are truncated after
  each database test.

## Consequences

Running tests needs Docker. Database tests fail fast with a clear message when
`TEST_DATABASE_URL` is not set.
