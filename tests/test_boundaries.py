"""Architecture rules that must hold for every module."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "sentinel"
SECURITY = PACKAGE / "security"
HTTP_LIBRARIES = {"httpx", "httpx2", "httpcore", "requests", "urllib3", "aiohttp"}


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_package_is_found() -> None:
    assert (SECURITY / "http.py").is_file()


def test_only_the_security_package_imports_http_clients() -> None:
    offenders = {
        str(path.relative_to(PACKAGE)): sorted(imported_roots(path) & HTTP_LIBRARIES)
        for path in PACKAGE.rglob("*.py")
        if SECURITY not in path.parents and imported_roots(path) & HTTP_LIBRARIES
    }
    assert offenders == {}, f"outbound HTTP must go through sentinel.security.http: {offenders}"


def test_detector_catches_a_direct_import(tmp_path: Path) -> None:
    sample = tmp_path / "bad.py"
    sample.write_text("import httpx.client\nfrom requests import get\n", encoding="utf-8")
    assert imported_roots(sample) >= {"httpx", "requests"}
