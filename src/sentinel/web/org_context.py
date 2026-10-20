"""Resolve ``/o/{slug}`` to the signed-in user's tenant scope (404 when not a member)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Path, Request
from starlette.exceptions import HTTPException

from sentinel.models import Organization
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import services
from sentinel.web.session_auth import Viewer, require_viewer


@dataclass(frozen=True)
class OrgContext:
    scope: TenantScope
    org: Organization
    viewer: Viewer


async def org_context(
    request: Request,
    slug: Annotated[str, Path(min_length=1, max_length=63)],
    viewer: Annotated[Viewer, Depends(require_viewer)],
) -> OrgContext:
    resolved = await services(request).orgs.resolve(viewer.user.id, slug)
    if resolved is None:
        # Same answer whether the organization does not exist or the user is not in it.
        raise HTTPException(status_code=404)
    scope, org = resolved
    return OrgContext(scope=scope, org=org, viewer=viewer)


InOrg = Annotated[OrgContext, Depends(org_context)]
