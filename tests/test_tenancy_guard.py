"""The tenant guard: scoped sessions see and write only their own organization."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select, update

from sentinel.db.session import Database
from sentinel.models import AuditEvent, Membership, Organization, User
from sentinel.tenancy.guard import TenantScopeMissingError, TenantViolationError
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope


async def make_org(db: Database, slug: str, members: int = 2) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with db.system("test fixture") as s:
        org = Organization(slug=slug, name=slug.upper())
        s.add(org)
        await s.flush()
        users = []
        for i in range(members):
            user = User(
                email=f"{slug}-{i}@example.invalid",
                display_name=f"{slug} {i}",
                password_hash="x",
                privacy_notice_version="test",
            )
            s.add(user)
            await s.flush()
            s.add(Membership(org_id=org.id, user_id=user.id, role="member"))
            users.append(user.id)
        s.add(AuditEvent(org_id=org.id, action=f"{slug}.created"))
        return org.id, users


def scope_for(org_id: uuid.UUID) -> TenantScope:
    return TenantScope(org_id=org_id, user_id=None, role=Role.OWNER)


@pytest.fixture
async def two_orgs(db: Database) -> tuple[uuid.UUID, uuid.UUID]:
    a, _ = await make_org(db, "alpha")
    b, _ = await make_org(db, "beta", members=3)
    async with db.system("test fixture") as s:
        s.add(AuditEvent(org_id=None, action="platform.event"))
    return a, b


async def test_unscoped_query_on_tenant_model_is_refused(db: Database) -> None:
    async with db.session() as s:
        with pytest.raises(TenantScopeMissingError, match="Membership"):
            await s.execute(select(Membership))


async def test_unscoped_session_can_read_non_tenant_tables(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async with db.session() as s:
        assert (await s.scalar(select(func.count()).select_from(User))) == 5


async def test_scoped_select_sees_only_own_rows(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = two_orgs
    async with db.tenant(scope_for(a)) as s:
        rows = (await s.scalars(select(Membership))).all()
    assert len(rows) == 2
    assert {m.org_id for m in rows} == {a}


async def test_explicit_filter_on_other_org_returns_nothing(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_orgs
    async with db.tenant(scope_for(a)) as s:
        assert (await s.scalars(select(Membership).where(Membership.org_id == b))).all() == []


async def test_get_by_primary_key_of_other_org_returns_none(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_orgs
    async with db.system("test lookup") as s:
        foreign_id = await s.scalar(select(Membership.id).where(Membership.org_id == b).limit(1))
    async with db.tenant(scope_for(a)) as s:
        assert await s.get(Membership, foreign_id) is None


async def test_join_through_tenant_table_is_scoped(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = two_orgs
    async with db.tenant(scope_for(a)) as s:
        emails = (await s.scalars(select(User.email).join(Membership))).all()
    assert sorted(emails) == ["alpha-0@example.invalid", "alpha-1@example.invalid"]


async def test_subquery_on_tenant_table_is_scoped(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = two_orgs
    members = select(Membership.user_id).scalar_subquery()
    async with db.tenant(scope_for(a)) as s:
        count = await s.scalar(select(func.count()).select_from(User).where(User.id.in_(members)))
    assert count == 2


async def test_unscoped_join_into_tenant_table_sees_no_tenant_rows(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async with db.session() as s:
        assert (await s.scalars(select(User.email).join(Membership))).all() == []


async def test_bulk_update_and_delete_are_scoped(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_orgs
    async with db.tenant(scope_for(a)) as s:
        await s.execute(update(Membership).values(role="viewer"))
    async with db.tenant(scope_for(a)) as s:
        await s.execute(delete(AuditEvent))
    async with db.system("test check") as s:
        roles_b = set(
            (await s.scalars(select(Membership.role).where(Membership.org_id == b))).all()
        )
        roles_a = set(
            (await s.scalars(select(Membership.role).where(Membership.org_id == a))).all()
        )
        events = (await s.scalars(select(AuditEvent.action))).all()
    assert roles_a == {"viewer"}
    assert roles_b == {"member"}
    assert sorted(events) == ["beta.created", "platform.event"]


async def test_scoped_session_never_sees_platform_level_rows(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = two_orgs
    async with db.tenant(scope_for(a)) as s:
        actions = (await s.scalars(select(AuditEvent.action))).all()
    assert actions == ["alpha.created"]


async def test_insert_into_another_org_is_refused(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_orgs
    with pytest.raises(TenantViolationError):
        async with db.tenant(scope_for(a)) as s:
            s.add(AuditEvent(org_id=b, action="sneaky"))


async def test_platform_row_cannot_be_written_from_a_tenant_session(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = two_orgs
    with pytest.raises(TenantViolationError):
        async with db.tenant(scope_for(a)) as s:
            s.add(AuditEvent(org_id=None, action="sneaky"))


async def test_moving_a_row_to_another_org_is_refused(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_orgs

    async def move_first_membership() -> None:
        async with db.tenant(scope_for(a)) as s:
            membership = (await s.scalars(select(Membership).limit(1))).one()
            membership.org_id = b

    with pytest.raises(TenantViolationError):
        await move_first_membership()


async def test_unscoped_write_is_refused(
    db: Database, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = two_orgs
    with pytest.raises(TenantScopeMissingError):
        async with db.session() as s:
            s.add(AuditEvent(org_id=a, action="unscoped"))


async def test_unscoped_session_may_write_platform_level_rows(db: Database) -> None:
    async with db.session() as s:
        s.add(AuditEvent(org_id=None, action="account.login"))
    async with db.system("test check") as s:
        assert (await s.scalars(select(AuditEvent.action))).all() == ["account.login"]


async def test_system_sessions_need_a_reason(db: Database) -> None:
    with pytest.raises(ValueError, match="reason"):
        async with db.system(""):
            pass
