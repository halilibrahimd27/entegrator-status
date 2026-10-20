"""``release``: published versions of a package, repository or image.

Each ecosystem through its documented, robots-allowed front door (ADR 0003, ADR 0010):

- ``github``: ``api.github.com`` releases (tags when a repository publishes none).
- ``npm``: the registry's abbreviated metadata (versions, dist-tags, deprecations).
- ``pypi``: the per-project releases RSS feed. PyPI's robots.txt disallows the JSON API
  (``/pypi/*/json``) for bots; the RSS feed is allowed.
- ``oci``: Docker Hub tags; only plain version tags count (``1.27-alpine`` is a variant).

Items keep the version, title, link, date and two flags: ``prerelease`` and
``breaking`` (the release notes mention a breaking change). Notes themselves are read
for that flag and then dropped.
"""

from __future__ import annotations

import json
import re
from functools import cmp_to_key
from typing import Any
from urllib.parse import quote

from sentinel.models.catalog import SourceKind
from sentinel.security.http import CONTRACT_MAX_BYTES, FetchResult
from sentinel.sources.adapters.base import Adapter, AdapterEnv, ProbeJob, ProbeOutcome
from sentinel.sources.adapters.content import fetch_content, parse_failure, snapshot_outcome
from sentinel.sources.adapters.feed import ACCEPT as FEED_ACCEPT
from sentinel.sources.feeds import FeedError, clean_title, parse_date, parse_feed, safe_link
from sentinel.sources.versions import Version, compare, parse

KEEP = 30
BREAKING = re.compile(
    r"breaking[ _-]?change|\bbreaking\b|backwards?[ -]incompatible|"
    r"kırıcı değişiklik|geriye dönük uyumsuz|uyumsuz değişiklik",
    re.IGNORECASE,
)
_PLAIN_TAG = re.compile(r"^[vV]?\d+(?:\.\d+){0,3}$")


def release_item(
    version: str,
    *,
    title: str | None = None,
    link: str | None = None,
    published: str | None = None,
    prerelease: bool = False,
    notes: str = "",
    **flags: bool,
) -> dict[str, Any]:
    parsed = parse(version)
    return {
        "id": f"v:{version[:100]}",
        "version": version[:100],
        "title": clean_title(title or version),
        "link": link,
        "published": published,
        "prerelease": bool(prerelease or (parsed is not None and parsed.is_prerelease)),
        "breaking": bool(BREAKING.search(notes)),
        **flags,
    }


Ranked = tuple[Version, dict[str, Any]]


def _newer_first(a: Ranked, b: Ranked) -> int:
    return compare(b[0], a[0])


def newest_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items whose version parses, newest first, at most ``KEEP``; the rest are dropped."""
    known: list[Ranked] = []
    for item in items:
        version = parse(str(item["version"]))
        if version is not None:
            known.append((version, item))
    known.sort(key=cmp_to_key(_newer_first))
    return [item for _, item in known][:KEEP]


def github(data: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ValueError("not a release list")
    items = []
    for release in data:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = str(release.get("tag_name") or release.get("name") or "")
        if not tag:
            continue
        link = safe_link(str(release.get("html_url") or ""), "https://github.com/")
        items.append(
            release_item(
                tag,
                title=release.get("name") if "tag_name" in release else None,
                link=link or f"https://github.com/{name}/releases/tag/{quote(tag)}",
                published=parse_date(str(release.get("published_at") or "")),
                prerelease=bool(release.get("prerelease")),
                notes=str(release.get("body") or ""),
            )
        )
    return newest_first(items)


def npm(data: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("versions"), dict):
        raise ValueError("not npm package metadata")
    latest = str((data.get("dist-tags") or {}).get("latest") or "")
    items = [
        release_item(
            version,
            link=f"https://www.npmjs.com/package/{name}/v/{quote(version)}",
            latest=version == latest,
            deprecated=bool(isinstance(meta, dict) and meta.get("deprecated")),
        )
        for version, meta in data["versions"].items()
    ]
    return newest_first(items)


def dockerhub(data: Any, repository: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("not a Docker Hub tag list")
    page = (
        f"https://hub.docker.com/_/{repository.removeprefix('library/')}"
        if repository.startswith("library/")
        else f"https://hub.docker.com/r/{repository}"
    )
    items = [
        release_item(
            str(tag["name"]),
            link=page,
            published=parse_date(str(tag.get("last_updated") or "")),
        )
        for tag in data["results"]
        if isinstance(tag, dict) and _PLAIN_TAG.match(str(tag.get("name", "")))
    ]
    return newest_first(items)


class ReleaseAdapter(Adapter):
    kind = SourceKind.RELEASE

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        ecosystem = str(job.config["ecosystem"])
        name = str(job.config["name"])
        if ecosystem == "github":
            return await self._github(job, env, name)
        if ecosystem == "npm":
            url = f"https://registry.npmjs.org/{name.replace('/', '%2F')}"
            accept = "application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8"
            return await self._json(job, env, url, accept, lambda d: npm(d, name))
        if ecosystem == "pypi":
            url = f"https://pypi.org/rss/project/{quote(name.lower())}/releases.xml"
            result = await fetch_content(job, env, url, FEED_ACCEPT)
            if isinstance(result, ProbeOutcome):
                return result
            try:
                entries = parse_feed(result.body, result.url)
            except FeedError as exc:
                return parse_failure(result, str(exc))
            items = [
                release_item(e["title"], link=e["link"], published=e["published"]) for e in entries
            ]
            return snapshot_outcome(result, newest_first(items))
        if ecosystem == "oci":
            repository = name if "/" in name else f"library/{name}"
            url = (
                f"https://hub.docker.com/v2/repositories/{repository}/tags"
                "?page_size=50&ordering=last_updated"
            )
            return await self._json(
                job, env, url, "application/json", lambda d: dockerhub(d, repository)
            )
        return ProbeOutcome(False, "unsupported", details={"ecosystem": ecosystem[:40]})

    async def _json(
        self, job: ProbeJob, env: AdapterEnv, url: str, accept: str, read: Any
    ) -> ProbeOutcome:
        result = await fetch_content(job, env, url, accept, max_bytes=CONTRACT_MAX_BYTES)
        if isinstance(result, ProbeOutcome):
            return result
        return self._items(result, read)

    @staticmethod
    def _items(result: FetchResult, read: Any) -> ProbeOutcome:
        try:
            items = read(json.loads(result.body))
        except (ValueError, KeyError, TypeError) as exc:
            return parse_failure(result, type(exc).__name__)
        return snapshot_outcome(result, items)

    async def _github(self, job: ProbeJob, env: AdapterEnv, name: str) -> ProbeOutcome:
        base = f"https://api.github.com/repos/{name}"
        accept = "application/vnd.github+json"
        result = await fetch_content(
            job, env, f"{base}/releases?per_page={KEEP}", accept, max_bytes=CONTRACT_MAX_BYTES
        )
        if isinstance(result, ProbeOutcome):
            return result
        try:
            data = json.loads(result.body)
        except ValueError as exc:
            return parse_failure(result, type(exc).__name__)
        if isinstance(data, list) and not data:  # no releases: fall back to tags
            tags = await fetch_content(
                job, env, f"{base}/tags?per_page={KEEP}", accept, conditional=False
            )
            if isinstance(tags, ProbeOutcome):
                return tags
            return self._items(tags, lambda d: github(d, name))
        return self._items(result, lambda d: github(d, name))
