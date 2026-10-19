"""The tenant scope a request or job runs under."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sentinel.tenancy.roles import Action, Role, can


class PermissionDeniedError(Exception):
    """The scope's role does not allow the action."""


@dataclass(frozen=True)
class TenantScope:
    org_id: uuid.UUID
    user_id: uuid.UUID | None  # None for API tokens and system jobs acting for the org
    role: Role

    def require(self, action: Action) -> None:
        if not can(self.role, action):
            raise PermissionDeniedError(action.value)
