"""Catalog pack format: load (safely), validate, and describe as JSON Schema."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sentinel.models.catalog import ServiceKind, SourceKind
from sentinel.security.addresses import parse_target
from sentinel.security.errors import URLPolicyError
from sentinel.sources.config import KIND_CONFIG, SourceConfigError, validate_source

MAX_PACK_BYTES = 1024 * 1024
SLUG = r"^[a-z0-9][a-z0-9-]{0,62}$"
TAG = r"^[a-z0-9][a-z0-9_-]{0,62}$"
REF = r"^[a-z0-9][a-z0-9-]{0,62}/[a-z0-9][a-z0-9-]{0,62}$"

Slug = Annotated[str, Field(pattern=SLUG)]
Tag = Annotated[str, Field(pattern=TAG)]
Ref = Annotated[str, Field(pattern=REF, description="service-slug/component-slug")]


class PackError(ValueError):
    """The file could not be read as a pack at all."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class PackSource(_Model):
    kind: SourceKind
    config: dict[str, Any]
    interval_s: int | None = None
    timeout_s: int | None = None
    allow_http: bool = False
    availability_ignores_robots: bool = False
    source: Annotated[str, Field(min_length=3, max_length=500)] = Field(
        description="Where the URL came from (page checked by hand, documentation link)."
    )


class PackComponent(_Model):
    slug: Slug
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(max_length=2000)] | None = None
    tags: list[Tag] = []
    sources: list[PackSource] = []


class PackService(_Model):
    slug: Slug
    name: Annotated[str, Field(min_length=1, max_length=120)]
    kind: ServiceKind
    homepage: Annotated[str, Field(max_length=500)] | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    tags: list[Tag] = []
    purl: Annotated[str, Field(pattern=r"^pkg:[a-z]+/.+$", max_length=255)] | None = None
    components: list[PackComponent] = Field(min_length=1)

    @field_validator("homepage")
    @classmethod
    def _homepage(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parse_target(value)
            except URLPolicyError as exc:
                raise ValueError(str(exc)) from None
        return value


class PackEdge(_Model):
    from_: Ref = Field(alias="from")
    to: Ref
    note: Annotated[str, Field(max_length=500)] | None = None


class PackTagRule(_Model):
    tag: Tag
    to: Ref


class Pack(_Model):
    id: Slug
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(max_length=2000)] | None = None
    services: list[PackService] = []
    edges: list[PackEdge] = []
    tag_rules: list[PackTagRule] = []


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses aliases (no entity-expansion style "YAML bombs")."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.composer.ComposerError(None, None, "YAML aliases are not allowed", None)
        return super().compose_node(parent, index)


def safe_yaml(text: str) -> Any:
    if len(text.encode()) > MAX_PACK_BYTES:
        raise PackError(f"larger than {MAX_PACK_BYTES} bytes")
    try:
        return yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506  # nosec B506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise PackError(f"invalid YAML: {exc}") from None


def load_pack(path: Path) -> Pack:
    data = safe_yaml(path.read_text(encoding="utf-8"))
    try:
        return Pack.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        )
        raise PackError(problems) from None


def validate_packs(packs: list[Pack]) -> list[str]:
    """Every problem across ``packs`` (they share one slug namespace)."""
    problems: list[str] = []
    seen_packs: set[str] = set()
    components: set[str] = set()
    services: dict[str, str] = {}
    for pack in packs:
        if pack.id in seen_packs:
            problems.append(f"{pack.id}: duplicate pack id")
        seen_packs.add(pack.id)
        for service in pack.services:
            if service.slug in services:
                problems.append(
                    f"{pack.id}/{service.slug}: service slug also in pack {services[service.slug]}"
                )
            services[service.slug] = pack.id
            local: set[str] = set()
            for component in service.components:
                ref = f"{service.slug}/{component.slug}"
                if component.slug in local:
                    problems.append(f"{pack.id}/{ref}: duplicate component slug")
                local.add(component.slug)
                components.add(ref)
                identities: set[str] = set()
                for i, source in enumerate(component.sources):
                    where = f"{pack.id}/{ref}#source{i + 1} ({source.kind.value})"
                    try:
                        valid = validate_source(
                            source.kind.value,
                            source.config,
                            interval_s=source.interval_s,
                            timeout_s=source.timeout_s,
                            allow_http=source.allow_http,
                        )
                    except SourceConfigError as exc:
                        problems.append(f"{where}: {exc}")
                        continue
                    if valid.identity in identities:
                        problems.append(f"{where}: duplicate source ({valid.identity})")
                    identities.add(valid.identity)

    edges: dict[str, set[str]] = {}
    for pack in packs:
        for edge in pack.edges:
            for ref in (edge.from_, edge.to):
                if ref not in components:
                    problems.append(
                        f"{pack.id}: edge {edge.from_} -> {edge.to}: unknown component {ref}"
                    )
            if edge.from_ == edge.to:
                problems.append(f"{pack.id}: edge {edge.from_} -> {edge.to}: self edge")
            edges.setdefault(edge.from_, set()).add(edge.to)
        for rule in pack.tag_rules:
            if rule.to not in components:
                problems.append(f"{pack.id}: tag rule {rule.tag} -> {rule.to}: unknown component")
    for pack in packs:  # tag rules become edges too
        for service in pack.services:
            for component in service.components:
                ref = f"{service.slug}/{component.slug}"
                for other in packs:
                    for rule in other.tag_rules:
                        if rule.tag in component.tags and rule.to != ref:
                            edges.setdefault(ref, set()).add(rule.to)
    cycle = _find_cycle(edges)
    if cycle:
        problems.append("cycle: " + " -> ".join(cycle))
    return problems


def _find_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    state: dict[str, int] = {}  # 1 = on the stack, 2 = done
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt) == 1:
                return [*stack[stack.index(nxt) :], nxt]
            if state.get(nxt) is None and (found := visit(nxt)):
                return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(edges):
        if state.get(node) is None and (found := visit(node)):
            return found
    return None


def pack_json_schema() -> dict[str, Any]:
    """JSON Schema for editors; each source kind gets its own ``config`` schema."""
    schema = Pack.model_json_schema(by_alias=True)
    defs = schema.setdefault("$defs", {})
    branches = []
    for kind, model in KIND_CONFIG.items():
        config_schema = model.model_json_schema()
        for name, definition in config_schema.pop("$defs", {}).items():
            defs.setdefault(name, definition)
        branches.append(
            {
                "if": {"properties": {"kind": {"const": kind.value}}},
                "then": {"properties": {"config": config_schema}},
            }
        )
    defs["PackSource"]["allOf"] = branches
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Entegratör Status catalog pack"
    return schema
