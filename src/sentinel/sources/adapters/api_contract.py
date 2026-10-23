"""``api_contract``: the shape of an API (OpenAPI, a JSON response, WSDL/XSD) as facts.

Fetched with the organization's credential when one is attached (ADR 0011), robots.txt
checked every cycle. WSDL imports are followed on the same host only and at most
``MAX_IMPORT_DEPTH`` levels deep; an import on another host is refused and recorded as
a fact, so the source keeps working and the refusal is visible.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from sentinel.contracts import jsonshape, openapi, wsdl
from sentinel.contracts.facts import ContractError
from sentinel.models.catalog import SourceKind
from sentinel.security.http import CONTRACT_MAX_BYTES, BoundCredential, FetchResult
from sentinel.sources.adapters.base import (
    Adapter,
    AdapterEnv,
    ProbeJob,
    ProbeOutcome,
    credential,
)
from sentinel.sources.adapters.content import fetch_content, parse_failure
from sentinel.sources.snapshots import SnapshotTooLargeError, make_snapshot

MAX_IMPORT_DEPTH = 3
MAX_DOCUMENTS = 10
ACCEPT = {
    "openapi": "application/json, application/yaml;q=0.9, text/yaml;q=0.9, */*;q=0.5",
    "json": "application/json",
    "wsdl": "text/xml, application/xml, application/wsdl+xml;q=0.9",
}
_TEMPLATE = re.compile(r"\{[^}/]*\}")


class ApiContractAdapter(Adapter):
    kind = SourceKind.API_CONTRACT

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        fmt = str(job.config["format"])
        url = str(job.config["url"])
        bound = await credential(job, env)
        result = await fetch_content(
            job,
            env,
            url,
            ACCEPT.get(fmt, "*/*"),
            max_bytes=CONTRACT_MAX_BYTES,
            conditional=fmt != "wsdl",  # imports can change while the main document does not
            credential=bound,
        )
        if isinstance(result, ProbeOutcome):
            return result
        try:
            if fmt == "openapi":
                items = openapi.normalise(openapi.load(result.body))
            elif fmt == "json":
                op = f"GET {_TEMPLATE.sub('{}', urlsplit(url).path or '/')}"
                items = jsonshape.shape(json.loads(result.body), op)
            else:
                items = await self._wsdl(job, env, result, bound)
        except (ContractError, ValueError) as exc:
            return parse_failure(result, str(exc))
        latency = int(result.elapsed_s * 1000)
        try:
            snapshot = make_snapshot(
                items,
                etag=result.headers.get("etag"),
                last_modified=result.headers.get("last-modified"),
                strict=True,
            )
        except SnapshotTooLargeError as exc:
            return ProbeOutcome(False, "response_too_large", 200, latency, {"reason": str(exc)})
        return ProbeOutcome(True, None, 200, latency, {"facts": len(items)}, snapshot=snapshot)

    async def _wsdl(
        self, job: ProbeJob, env: AdapterEnv, first: FetchResult, bound: BoundCredential | None
    ) -> list[dict[str, object]]:
        host = urlsplit(first.url).hostname
        documents = [wsdl.parse(first.body)]
        refused: list[str] = []
        seen = {first.url}
        frontier = [(u, 1) for u in wsdl.imports(documents[0], first.url)]
        while frontier and len(documents) < MAX_DOCUMENTS:
            url, depth = frontier.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if urlsplit(url).hostname != host:
                refused.append(url)
                continue
            if depth > MAX_IMPORT_DEPTH:
                continue
            fetched = await fetch_content(
                job, env, url, ACCEPT["wsdl"], max_bytes=CONTRACT_MAX_BYTES,
                conditional=False, credential=bound,
            )  # fmt: skip
            if isinstance(fetched, ProbeOutcome):
                raise ContractError(f"import unavailable: {fetched.error_class}")
            root = wsdl.parse(fetched.body)
            documents.append(root)
            frontier.extend((u, depth + 1) for u in wsdl.imports(root, fetched.url))
        items: list[dict[str, object]] = list(wsdl.normalise(documents))
        items.extend({"id": f"import:{u}"[:500], "t": "import", "refused": True} for u in refused)
        return items
