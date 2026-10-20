"""Tenant guard: scoped sessions only ever see and write their own organization's rows.

Two layers, both on the ORM session:

1. ``do_orm_execute`` adds ``org_id = :scope`` criteria to every SELECT, UPDATE and
   DELETE that touches a tenant-owned model, and raises when there is no scope.
2. ``before_flush`` checks that every new, changed or deleted tenant-owned object
   belongs to the scope's organization.

System code that must cross tenants (purge jobs, sign-in lookups) opens a session with
an explicit, named bypass. Raw SQL against tenant tables is not allowed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, false
from sqlalchemy.orm import ORMExecuteState, Session, UOWTransaction, with_loader_criteria

from sentinel.db.base import OptionalTenantMixin, TenantOwned
from sentinel.tenancy.scope import TenantScope

SCOPE_KEY = "tenant_scope"
BYPASS_KEY = "tenant_bypass"


class TenantScopeMissingError(RuntimeError):
    """A tenant-owned model was queried or written without a tenant scope."""


class TenantViolationError(RuntimeError):
    """A write targeted another organization's row."""


class GuardedSession(Session):
    """Sync session class behind every ``AsyncSession`` the app creates."""


def _tenant_entities(state: ORMExecuteState) -> list[str]:
    return [
        mapper.class_.__name__
        for mapper in state.all_mappers
        if issubclass(mapper.class_, TenantOwned)
    ]


@event.listens_for(GuardedSession, "do_orm_execute")
def _scope_statements(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.is_column_load or state.is_relationship_load:
        return  # follow-up loads inherit the criteria of the statement that loaded the parent
    if state.session.info.get(BYPASS_KEY):
        return
    scope: TenantScope | None = state.session.info.get(SCOPE_KEY)
    if scope is None:
        entities = _tenant_entities(state)
        if entities:
            raise TenantScopeMissingError(f"query on {', '.join(entities)} without a tenant scope")
        # Tenant tables reached only through joins or subqueries: show no rows at all.
        state.statement = state.statement.options(
            with_loader_criteria(TenantOwned, lambda cls: false(), include_aliases=True)
        )
        return
    # Always applied (not only when the selected entity is tenant-owned): the criteria
    # also reach tenant tables that appear only in joins and subqueries.
    org_id = scope.org_id
    state.statement = state.statement.options(
        with_loader_criteria(TenantOwned, lambda cls: cls.org_id == org_id, include_aliases=True)
    )


@event.listens_for(GuardedSession, "before_flush")
def _check_writes(session: Session, flush_context: UOWTransaction, instances: Any) -> None:
    if session.info.get(BYPASS_KEY):
        return
    scope: TenantScope | None = session.info.get(SCOPE_KEY)
    for obj in (*session.new, *session.dirty, *session.deleted):
        if not isinstance(obj, TenantOwned):
            continue
        name = type(obj).__name__
        if scope is None:
            if isinstance(obj, OptionalTenantMixin) and obj.org_id is None:
                continue  # platform-level row (e.g. a sign-in audit event): no tenant to leak
            raise TenantScopeMissingError(f"write to {name} without a tenant scope")
        if obj.org_id != scope.org_id:  # type: ignore[attr-defined]
            raise TenantViolationError(f"write to {name} outside the scope's organization")
