"""``advisory``: known vulnerabilities of a package, from OSV.dev.

``POST /v1/query`` with the package (no version) returns every advisory with its
affected ranges; the engine decides which pinned versions are affected. The query is
the read-query exception of ADR 0003 rule 2: one fixed endpoint, a body we build, no
redirects followed. robots.txt is checked like for every content source.
"""

from __future__ import annotations

import json
from typing import Any

from sentinel.models.catalog import SourceKind
from sentinel.security.http import CONTRACT_MAX_BYTES, FetchPolicy, Timeouts
from sentinel.sources.adapters.base import Adapter, AdapterEnv, ProbeJob, ProbeOutcome
from sentinel.sources.adapters.content import parse_failure, robots_refusal, snapshot_outcome
from sentinel.sources.feeds import clean_title, parse_date

OSV_QUERY = "https://api.osv.dev/v1/query"
# our config names → OSV ecosystem names
ECOSYSTEMS = {
    "npm": "npm",
    "pypi": "PyPI",
    "go": "Go",
    "maven": "Maven",
    "nuget": "NuGet",
    "packagist": "Packagist",
    "rubygems": "RubyGems",
    "crates.io": "crates.io",
}
SEVERITIES = ("LOW", "MODERATE", "MEDIUM", "HIGH", "CRITICAL")
MAX_VERSIONS = 200


def severity_of(vuln: dict[str, Any]) -> str | None:
    """The database's own label (GHSA style), else ``None``; CVSS vectors are kept raw."""
    label = str((vuln.get("database_specific") or {}).get("severity", "")).upper()
    if label == "MEDIUM":
        label = "MODERATE"
    return label if label in SEVERITIES else None


def advisories(data: Any, name: str, ecosystem: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("not an OSV answer")
    items = []
    for vuln in data.get("vulns") or []:
        if not isinstance(vuln, dict) or not vuln.get("id") or vuln.get("withdrawn"):
            continue
        ranges: list[dict[str, str]] = []
        versions: list[str] = []
        for affected in vuln.get("affected") or []:
            package = affected.get("package") or {}
            if package.get("name") != name or package.get("ecosystem") != ecosystem:
                continue
            versions.extend(str(v) for v in affected.get("versions") or [])
            for found in affected.get("ranges") or []:
                if found.get("type") not in ("SEMVER", "ECOSYSTEM"):
                    continue  # GIT ranges name commits, not versions
                for event in found.get("events") or []:
                    for key in ("introduced", "fixed", "last_affected"):
                        if key in event:
                            ranges.append({key: str(event[key])[:100]})
        vuln_id = str(vuln["id"])[:100]
        items.append(
            {
                "id": vuln_id,
                "title": clean_title(str(vuln.get("summary") or vuln_id)),
                "link": f"https://osv.dev/vulnerability/{vuln_id}",
                "published": parse_date(str(vuln.get("published") or "")),
                "severity": severity_of(vuln),
                "aliases": [str(a)[:60] for a in (vuln.get("aliases") or [])][:10],
                "events": ranges,
                "versions": versions[:MAX_VERSIONS],
            }
        )
    return items


class AdvisoryAdapter(Adapter):
    kind = SourceKind.ADVISORY

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        ecosystem = ECOSYSTEMS.get(str(job.config["ecosystem"]))
        if ecosystem is None:
            return ProbeOutcome(
                False, "unsupported", details={"ecosystem": str(job.config["ecosystem"])[:40]}
            )
        name = str(job.config["name"])
        if refusal := await robots_refusal(job, env, OSV_QUERY):
            return refusal
        timeout = float(job.timeout_s)
        body = json.dumps({"package": {"name": name, "ecosystem": ecosystem}}).encode()
        policy = FetchPolicy(
            method="POST",
            json_body=body,
            read_query=True,
            max_redirects=0,
            max_bytes=CONTRACT_MAX_BYTES,
            timeouts=Timeouts(connect_s=min(5.0, timeout), read_s=timeout, total_s=timeout * 2),
            headers={"Accept": "application/json"},
        )
        result = await env.http.fetch(OSV_QUERY, policy)
        if result.status_code != 200:
            status = result.status_code
            error = "http_5xx" if status >= 500 else "http_4xx"
            return ProbeOutcome(False, error, status, int(result.elapsed_s * 1000))
        try:
            items = advisories(json.loads(result.body), name, ecosystem)
        except ValueError as exc:
            return parse_failure(result, str(exc))
        if not items:  # no known advisories is a result, not a broken source
            items = [{"id": "none", "type": "none"}]
        return snapshot_outcome(result, items)
