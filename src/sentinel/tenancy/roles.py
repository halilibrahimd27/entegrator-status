"""Roles and the permission matrix. Every route checks permissions through ``can``."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Action(StrEnum):
    ORG_VIEW = "org.view"
    ORG_UPDATE = "org.update"
    ORG_DELETE = "org.delete"
    MEMBERS_VIEW = "members.view"
    MEMBERS_INVITE = "members.invite"
    MEMBERS_CHANGE_ROLE = "members.change_role"
    MEMBERS_REMOVE = "members.remove"
    TOKENS_MANAGE = "tokens.manage"
    AUDIT_VIEW = "audit.view"
    GRAPH_VIEW = "graph.view"
    GRAPH_EDIT = "graph.edit"
    STATUS_MANAGE = "status.manage"  # manual incidents, maintenance windows
    CREDENTIALS_MANAGE = "credentials.manage"  # write-only secrets (ADR 0011)


_VIEW = {Action.ORG_VIEW, Action.MEMBERS_VIEW, Action.GRAPH_VIEW}
_MEMBER = _VIEW | {Action.GRAPH_EDIT, Action.STATUS_MANAGE}
_ADMIN = _MEMBER | {
    Action.ORG_UPDATE,
    Action.MEMBERS_INVITE,
    Action.MEMBERS_CHANGE_ROLE,
    Action.MEMBERS_REMOVE,
    Action.TOKENS_MANAGE,
    Action.AUDIT_VIEW,
    Action.CREDENTIALS_MANAGE,
}
_OWNER = _ADMIN | {Action.ORG_DELETE}

PERMISSIONS: dict[Role, frozenset[Action]] = {
    Role.VIEWER: frozenset(_VIEW),
    Role.MEMBER: frozenset(_MEMBER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_OWNER),
}

# Who may grant which role: nobody can hand out more power than they hold, and only
# owners create owners.
GRANTABLE: dict[Role, frozenset[Role]] = {
    Role.OWNER: frozenset(Role),
    Role.ADMIN: frozenset({Role.ADMIN, Role.MEMBER, Role.VIEWER}),
    Role.MEMBER: frozenset(),
    Role.VIEWER: frozenset(),
}


def can(role: Role, action: Action) -> bool:
    return action in PERMISSIONS[role]


def can_grant(granter: Role, role: Role) -> bool:
    return role in GRANTABLE[granter]
