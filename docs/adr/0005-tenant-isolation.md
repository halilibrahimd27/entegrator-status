# ADR 0005 — Tenant isolation in the ORM session

- Status: accepted
- Date: 2026-10-19

## Context

Every organization's graph, results, credentials and alerts must stay invisible to every
other organization (CLAUDE.md "Tenancy and isolation"). The usual failure is one
forgotten `WHERE org_id = …` in one query, which code review does not reliably catch.

## Decision

Isolation is enforced by the ORM session, not by each query:

- Tenant-owned models inherit `TenantMixin` (or `OptionalTenantMixin` for rows that may
  be platform-level, such as audit events).
- `Database.tenant(scope)` opens a session whose every ORM `SELECT`, `UPDATE` and
  `DELETE` gets `org_id = :scope` through `with_loader_criteria` — including tenant
  tables that appear only in joins and subqueries.
- `Database.session()` (no scope) refuses queries whose result entity is tenant-owned and
  shows no tenant rows through joins.
- `before_flush` refuses any new, changed or deleted tenant row outside the scope,
  including platform-level rows written from a tenant session.
- `Database.system(reason)` is the only way to cross tenants (purge jobs, sign-in
  lookups, platform admin) and must name its reason.
- Raw SQL against tenant tables is not allowed.

PostgreSQL row-level security is **not** added now. The guard is tested directly
(`tests/test_tenancy_guard.py`) and every route is covered by the two-tenant harness;
RLS would add a second policy language and per-connection settings for little extra
assurance at this size. Revisit if raw SQL, reporting connections or a second service
writing to the database appear.

## Consequences

- Repository code reads like ordinary SQLAlchemy; forgetting the filter is not possible.
- Code that legitimately crosses tenants is easy to find (`grep -n "db.system("`).
