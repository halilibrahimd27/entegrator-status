"""Source adapters. Each kind registers one adapter; the prober looks them up by kind."""

from sentinel.sources.adapters.base import (
    REGISTRY,
    Adapter,
    AdapterEnv,
    ProbeJob,
    ProbeOutcome,
    adapter_for,
)
from sentinel.sources.adapters.dns import DnsAdapter
from sentinel.sources.adapters.domain_expiry import DomainExpiryAdapter
from sentinel.sources.adapters.feed import FeedAdapter
from sentinel.sources.adapters.http import HttpAdapter
from sentinel.sources.adapters.page_watch import PageWatchAdapter
from sentinel.sources.adapters.release import ReleaseAdapter
from sentinel.sources.adapters.status_page import StatusPageAdapter
from sentinel.sources.adapters.tls_cert import TlsCertAdapter

for _adapter in (
    HttpAdapter(),
    TlsCertAdapter(),
    DnsAdapter(),
    DomainExpiryAdapter(),
    FeedAdapter(),
    StatusPageAdapter(),
    PageWatchAdapter(),
    ReleaseAdapter(),
):
    REGISTRY[_adapter.kind] = _adapter

__all__ = ["REGISTRY", "Adapter", "AdapterEnv", "ProbeJob", "ProbeOutcome", "adapter_for"]
