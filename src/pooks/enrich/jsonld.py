"""schema.org JSON-LD extraction.

Both Goodreads and AbeBooks embed structured data intended for machine
consumption. Reading it is markedly more stable than parsing their HTML, which
is React-rendered and changes without notice.
"""

from __future__ import annotations

import json
import re
from typing import Any

_SCRIPT = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def extract_blocks(html: str) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page, flattened out of @graph and arrays."""
    blocks: list[dict[str, Any]] = []
    for match in _SCRIPT.finditer(html):
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        blocks.extend(_flatten(parsed))
    return blocks


def _flatten(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [item for entry in node for item in _flatten(entry)]
    if isinstance(node, dict):
        found = [node]
        if graph := node.get("@graph"):
            found.extend(_flatten(graph))
        return found
    return []


def find_by_type(blocks: list[dict[str, Any]], type_name: str) -> list[dict[str, Any]]:
    """Blocks whose @type matches, tolerating @type given as a list."""
    matched = []
    for block in blocks:
        declared = block.get("@type")
        names = declared if isinstance(declared, list) else [declared]
        if type_name in names:
            matched.append(block)
    return matched


def first_offer(item: dict[str, Any]) -> dict[str, Any]:
    """The `offers` value, which may arrive as a dict or a list."""
    offers = item.get("offers") or {}
    if isinstance(offers, list):
        return offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def as_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None
