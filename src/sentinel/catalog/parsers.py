"""Turn SBOMs, lockfiles and compose files into a list of packages (purls).

Uploaded content is parsed in-process with safe parsers, size-capped and never executed
or stored; only the resulting package list is kept (for the import preview).
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

import yaml

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 5000

# purl type -> ecosystem names used by source configs (release / advisory)
RELEASE_ECOSYSTEM = {"npm": "npm", "pypi": "pypi", "docker": "oci", "github": "github"}
ADVISORY_ECOSYSTEM = {
    "npm": "npm",
    "pypi": "pypi",
    "golang": "go",
    "maven": "maven",
    "nuget": "nuget",
    "composer": "packagist",
    "gem": "rubygems",
    "cargo": "crates.io",
}


class ImportFormatError(ValueError):
    """``key`` is the i18n key shown to the user."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class Purl:
    type: str
    namespace: str | None
    name: str
    version: str | None

    @property
    def base(self) -> str:
        """Without version: the identity a catalog service is matched on."""
        parts = [quote(p, safe="@") for p in (self.namespace.split("/") if self.namespace else [])]
        return "pkg:" + "/".join([self.type, *parts, quote(self.name, safe="")])

    @property
    def display(self) -> str:
        return f"{self.namespace}/{self.name}" if self.namespace else self.name


_PURL = re.compile(
    r"^pkg:(?P<type>[A-Za-z][A-Za-z0-9.+-]*)/(?P<path>[^@?#]+)(?:@(?P<version>[^?#]+))?"
)


def parse_purl(text: str) -> Purl | None:
    match = _PURL.match(text.strip())
    if not match:
        return None
    kind = match["type"].lower()
    segments = [unquote(s) for s in match["path"].strip("/").split("/") if s]
    if not segments:
        return None
    name, namespace = segments[-1], "/".join(segments[:-1]) or None
    if kind == "pypi":
        name = re.sub(r"[-_.]+", "-", name).lower()
    version = unquote(match["version"]) if match["version"] else None
    return Purl(type=kind, namespace=namespace, name=name, version=version)


def make_purl(kind: str, name: str, version: str | None) -> Purl | None:
    segments = [s for s in name.split("/") if s]
    if not segments:
        return None
    # "@" separates the version in a purl, so "@types/node" must become "%40types/node".
    path = "/".join(quote(segment, safe="") for segment in segments)
    suffix = f"@{quote(version, safe='')}" if version else ""
    return parse_purl(f"pkg:{kind}/{path}{suffix}")


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        raise ImportFormatError("imports.unreadable") from None


def _unique(items: list[Purl]) -> list[Purl]:
    seen: dict[str, Purl] = {}
    for item in items:
        seen.setdefault(f"{item.base}@{item.version}", item)
    if len(seen) > MAX_ITEMS:
        raise ImportFormatError("imports.too_many")
    return list(seen.values())


def from_cyclonedx(data: dict[str, Any]) -> list[Purl]:
    found: list[Purl] = []

    def walk(components: Any) -> None:
        for component in components if isinstance(components, list) else []:
            if isinstance(component, dict):
                purl = parse_purl(str(component.get("purl", "")))
                if purl:
                    found.append(purl)
                walk(component.get("components"))

    walk(data.get("components"))
    return found


def from_spdx(data: dict[str, Any]) -> list[Purl]:
    found: list[Purl] = []
    for package in data.get("packages") or []:
        for ref in package.get("externalRefs") or [] if isinstance(package, dict) else []:
            if isinstance(ref, dict) and ref.get("referenceType") == "purl":
                purl = parse_purl(str(ref.get("referenceLocator", "")))
                if purl:
                    found.append(purl)
    return found


def from_package_lock(data: dict[str, Any]) -> list[Purl]:
    found: list[Purl] = []
    for key, entry in (data.get("packages") or {}).items():  # lockfile v2 / v3
        if not key or not isinstance(entry, dict) or entry.get("link"):
            continue
        name = entry.get("name") or key.rsplit("node_modules/", 1)[-1]
        purl = make_purl("npm", str(name), entry.get("version"))
        if purl:
            found.append(purl)
    if not found:  # lockfile v1

        def walk(deps: Any) -> None:
            for name, entry in (deps or {}).items():
                if isinstance(entry, dict):
                    purl = make_purl("npm", str(name), entry.get("version"))
                    if purl:
                        found.append(purl)
                    walk(entry.get("dependencies"))

        walk(data.get("dependencies"))
    return found


_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(?:==\s*(?P<version>[A-Za-z0-9.!+_-]+))?"
)


def from_requirements(text: str) -> list[Purl]:
    found: list[Purl] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().rstrip("\\").strip()
        if not line or line.startswith(("-", "--")):
            continue
        match = _REQUIREMENT.match(line)
        if match:
            purl = make_purl("pypi", match["name"], match["version"])
            if purl:
                found.append(purl)
    return found


def from_poetry_lock(text: str) -> list[Purl]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ImportFormatError("imports.unreadable") from None
    found = []
    for package in data.get("package") or []:
        purl = make_purl("pypi", str(package.get("name", "")), package.get("version"))
        if purl:
            found.append(purl)
    return found


_IMAGE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9._/-]*?)(?::(?P<tag>[A-Za-z0-9._-]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)


def from_compose(text: str) -> list[Purl]:
    try:
        # Compose files use anchors/aliases legitimately; only the top level is read.
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ImportFormatError("imports.unreadable") from None
    found = []
    services = data.get("services") if isinstance(data, dict) else None
    for service in (services or {}).values():
        image = service.get("image") if isinstance(service, dict) else None
        match = _IMAGE.match(str(image)) if image else None
        if match:
            purl = make_purl("docker", match["name"], match["tag"] or "latest")
            if purl:
                found.append(purl)
    return found


def parse_upload(filename: str, content: bytes) -> list[Purl]:
    """Detect the format from the name and content, return unique packages."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise ImportFormatError("imports.too_large")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ImportFormatError("imports.unreadable") from None
    name = filename.lower().rsplit("/", 1)[-1]
    if name == "package-lock.json":
        items = from_package_lock(_json(text))
    elif name == "poetry.lock":
        items = from_poetry_lock(text)
    elif name.startswith("requirements") and name.endswith(".txt"):
        items = from_requirements(text)
    elif name.endswith((".yml", ".yaml")) and ("compose" in name):
        items = from_compose(text)
    elif name.endswith(".json"):
        data = _json(text)
        if not isinstance(data, dict):
            raise ImportFormatError("imports.unknown_format")
        if data.get("bomFormat") == "CycloneDX":
            items = from_cyclonedx(data)
        elif "spdxVersion" in data:
            items = from_spdx(data)
        else:
            raise ImportFormatError("imports.unknown_format")
    else:
        raise ImportFormatError("imports.unknown_format")
    items = _unique(items)
    if not items:
        raise ImportFormatError("imports.empty")
    return items
