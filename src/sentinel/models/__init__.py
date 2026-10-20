"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from sentinel.models.accounts import OneTimeToken, TokenPurpose, User, UserSession
from sentinel.models.audit import AuditEvent
from sentinel.models.catalog import (
    CatalogProposal,
    CatalogTagRule,
    Component,
    Dependency,
    Product,
    ProposalStatus,
    Service,
    ServiceKind,
    Source,
    SourceKind,
)
from sentinel.models.orgs import ApiToken, Invitation, Membership, Organization

__all__ = [
    "ApiToken",
    "AuditEvent",
    "CatalogProposal",
    "CatalogTagRule",
    "Component",
    "Dependency",
    "Invitation",
    "Membership",
    "OneTimeToken",
    "Organization",
    "Product",
    "ProposalStatus",
    "Service",
    "ServiceKind",
    "Source",
    "SourceKind",
    "TokenPurpose",
    "User",
    "UserSession",
]
