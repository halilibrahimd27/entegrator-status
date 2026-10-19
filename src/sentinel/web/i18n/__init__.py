"""User-facing copy. Templates read strings from here, never hard-code them."""

from __future__ import annotations

from functools import cache
from importlib import resources
from typing import Any

import yaml


@cache
def messages(locale: str = "tr") -> dict[str, str]:
    """Flattened ``{"site.disclaimer": "..."}`` map for ``locale``."""
    raw = resources.files(__package__).joinpath(f"{locale}.yaml").read_text(encoding="utf-8")
    flat: dict[str, str] = {}
    _flatten(yaml.safe_load(raw), "", flat)
    return flat


def t(key: str, locale: str = "tr") -> str:
    return messages(locale)[key]


def _flatten(node: Any, prefix: str, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten(value, f"{prefix}{key}.", out)
    elif isinstance(node, str):
        out[prefix.rstrip(".")] = node
    else:
        raise TypeError(f"{prefix.rstrip('.') or '<root>'}: values must be strings")
