"""Supply chain: every GitHub Action is pinned to a full commit SHA."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def test_actions_are_pinned_by_sha() -> None:
    found = {path.name: USES.findall(path.read_text()) for path in sorted(WORKFLOWS.glob("*.yml"))}
    assert {"ci.yml", "release.yml"} <= set(found)
    loose = [(name, ref) for name, refs in found.items() for ref in refs if not PINNED.match(ref)]
    assert loose == []


def test_release_only_pushes_from_tags() -> None:
    text = (WORKFLOWS / "release.yml").read_text()
    assert "push: ${{ github.ref_type == 'tag' }}" in text
    assert "sbom: true" in text
    assert "provenance: mode=max" in text
