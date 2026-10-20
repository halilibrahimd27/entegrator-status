"""Server-rendered SVG of a product's dependency graph (no JavaScript, CSP-safe).

Columns by depth: the product, then each hop. Styling comes from classes in app.css;
there are no style attributes. Every label is escaped.
"""

from __future__ import annotations

import uuid

from markupsafe import Markup, escape

from sentinel.graph.service import Subgraph

COL_W = 250
NODE_W = 210
NODE_H = 46
ROW_GAP = 14
PAD = 16
MAX_LABEL = 30


def _short(text: str) -> str:
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1] + "…"


def render(product_name: str, graph: Subgraph, *, title: str, description: str) -> Markup:
    columns: dict[int, list[uuid.UUID]] = {}
    for node in sorted(
        graph.nodes.values(), key=lambda n: (n.service.lower(), n.component.lower())
    ):
        columns.setdefault(node.depth, []).append(node.component_id)
    position: dict[uuid.UUID, tuple[int, int]] = {graph.product_id: (PAD, PAD)}
    for depth, ids in columns.items():
        for row, component_id in enumerate(ids):
            position[component_id] = (PAD + depth * COL_W, PAD + row * (NODE_H + ROW_GAP))
    rows = max([1, *(len(ids) for ids in columns.values())])
    width = PAD * 2 + NODE_W + COL_W * max([0, *columns])
    height = PAD * 2 + rows * (NODE_H + ROW_GAP) - ROW_GAP

    parts = [
        f'<svg class="graph" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-labelledby="graph-title graph-desc" '
        'xmlns="http://www.w3.org/2000/svg">',
        f'<title id="graph-title">{escape(title)}</title>',
        f'<desc id="graph-desc">{escape(description)}</desc>',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        '<path class="arrow" d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>',
    ]
    for edge in graph.edges:
        if edge.origin not in position or edge.target not in position:
            continue
        (x1, y1), (x2, y2) = position[edge.origin], position[edge.target]
        sx, sy, ex, ey = x1 + NODE_W, y1 + NODE_H // 2, x2, y2 + NODE_H // 2
        mid = (sx + ex) // 2
        kind = "shared" if edge.shared else "own"
        curve = f"M {sx} {sy} C {mid} {sy}, {mid} {ey}, {ex} {ey}"
        parts.append(f'<path class="edge {kind}" d="{curve}" marker-end="url(#arrow)"/>')
    parts.append(_box("product", *position[graph.product_id], [("label", 28, product_name)]))
    for node in graph.nodes.values():
        kind = "shared" if node.shared else "own"
        lines = [("service", 19, node.service), ("component", 36, node.component)]
        parts.append(_box(kind, *position[node.component_id], lines))
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704  # nosec B704 - every label is escaped


def _box(kind: str, x: int, y: int, lines: list[tuple[str, int, str]]) -> str:
    texts = "".join(
        f'<text x="{x + 12}" y="{y + dy}" class="{cls}">{escape(_short(text))}</text>'
        for cls, dy, text in lines
    )
    rect = f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="8"/>'
    return f'<g class="node {kind}">{rect}{texts}</g>'
