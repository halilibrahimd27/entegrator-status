"""YAML from strangers: SafeLoader without aliases (no "YAML bomb" expansion)."""

from __future__ import annotations

from typing import Any

import yaml


class NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses aliases, so a small document cannot expand to gigabytes."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.composer.ComposerError(None, None, "YAML aliases are not allowed", None)
        return super().compose_node(parent, index)


def load(text: str) -> Any:
    """Parse ``text``; raises ``yaml.YAMLError`` (aliases included)."""
    return yaml.load(text, Loader=NoAliasLoader)  # noqa: S506  # nosec B506 - SafeLoader subclass
