from __future__ import annotations

import uuid
from itertools import pairwise

import pytest

from sentinel.tenancy.roles import PERMISSIONS, Action, Role, can, can_grant
from sentinel.tenancy.scope import PermissionDeniedError, TenantScope

ORDER = [Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER]


def test_each_role_includes_the_one_below() -> None:
    for lower, higher in pairwise(ORDER):
        assert PERMISSIONS[lower] < PERMISSIONS[higher]


@pytest.mark.parametrize(
    ("role", "action", "allowed"),
    [
        (Role.VIEWER, Action.GRAPH_VIEW, True),
        (Role.VIEWER, Action.GRAPH_EDIT, False),
        (Role.MEMBER, Action.GRAPH_EDIT, True),
        (Role.MEMBER, Action.MEMBERS_INVITE, False),
        (Role.ADMIN, Action.TOKENS_MANAGE, True),
        (Role.ADMIN, Action.ORG_DELETE, False),
        (Role.OWNER, Action.ORG_DELETE, True),
    ],
)
def test_matrix(role: Role, action: Action, allowed: bool) -> None:
    assert can(role, action) is allowed


def test_nobody_grants_more_than_they_hold() -> None:
    assert can_grant(Role.OWNER, Role.OWNER)
    assert not can_grant(Role.ADMIN, Role.OWNER)
    assert can_grant(Role.ADMIN, Role.VIEWER)
    assert not any(can_grant(Role.MEMBER, r) for r in Role)


def test_scope_require() -> None:
    scope = TenantScope(org_id=uuid.uuid4(), user_id=None, role=Role.VIEWER)
    scope.require(Action.ORG_VIEW)
    with pytest.raises(PermissionDeniedError, match=r"graph\.edit"):
        scope.require(Action.GRAPH_EDIT)
