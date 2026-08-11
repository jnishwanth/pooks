"""How good is a cached answer, and can we do better?

Enrichment falls back when a source is throttled, and until now that fallback
was permanent: a rating scraped from Open Library (documented as too noisy to
rank on) was cached exactly as long as one from Goodreads, and a book whose
price lookup was blocked was cached forever with no price at all. Five of nine
rows in the first real database were frozen that way.

This module supplies the missing notion — which tier of source actually answered
— so the cache can expire in proportion to how good the answer was, and a repair
pass can revisit the worst records first.

Tiers are derived from what is already stored (`rating_source`,
`in_price_source`); no extra bookkeeping is written at enrichment time.
"""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Row
from typing import Any

# Indian price sources, best first. Amazon has by far the best catalogue
# coverage and the cleanest markup; the fixed retailers are small shops;
# SearXNG discovery is whatever happened to surface.
PRICE_TIERS: dict[str, int] = {
    "amazon.in": 0,
    "bookswagon": 1,
    "bookstohome": 1,
    "thebookx": 1,
}
SEARXNG_TIER = 2
UNKNOWN_TIER = 99  # blocked: we never found out


@dataclass(frozen=True)
class Quality:
    """Tier of each half of a record. Lower is better; None means absent."""

    rating_tier: int | None
    price_tier: int | None
    price_unknown: bool
    degraded: bool
    tags_unasked: bool = False

    @property
    def rating_is_best(self) -> bool:
        return self.rating_tier == 0

    @property
    def price_is_best(self) -> bool:
        return self.price_tier == 0


def rating_tier(source: str | None, chain: list[str]) -> int | None:
    """Position in the configured chain, so reordering it redefines 'better'."""
    if not source:
        return None
    try:
        return chain.index(source)
    except ValueError:
        # A source no longer in the chain — treat as worst rather than absent,
        # since the value is real but nothing would pick it again.
        return len(chain)


def price_tier(source: str | None) -> int | None:
    if not source:
        return None
    if source.startswith("searxng:"):
        return SEARXNG_TIER
    return PRICE_TIERS.get(source, SEARXNG_TIER)


def assess(row: Row | dict[str, Any], chain: list[str], provenance: dict[str, Any]) -> Quality:
    return Quality(
        rating_tier=rating_tier(row["rating_source"], chain),
        price_tier=price_tier(row["in_price_source"]),
        price_unknown=bool(row["in_price_unknown"]),
        degraded=bool(provenance.get("degraded_hosts")),
        # NULL means Hardcover was never asked. '{}' means it was, and has none
        # for this book — settled for ~2 in 5, so retrying it is pure waste.
        tags_unasked=row["tags_json"] is None,
    )


def improvable(quality: Quality, *, price_available: bool | None) -> tuple[bool, str]:
    """Whether a better answer is plausibly obtainable now.

    `price_available` is the stored `in_available` flag. It matters because
    "no Indian retailer stocks this" is a *complete* answer — retrying it would
    be pure waste — whereas "we were blocked before we could find out" is not.
    """
    if quality.price_unknown:
        return True, "price lookup was blocked"
    if quality.degraded:
        return True, "a source was throttled during enrichment"
    if quality.tags_unasked:
        return True, "tags never fetched"
    if quality.rating_tier is None:
        return True, "no rating found"
    if quality.rating_tier > 0:
        return True, "rating came from a fallback source"
    if quality.price_tier is not None and quality.price_tier > 0:
        return True, "price came from a fallback source"
    if quality.price_tier is None and price_available is None:
        return True, "price never determined"
    return False, "already from primary sources"


def is_better(new: int | None, old: int | None) -> bool:
    """Tier comparison where None means 'nothing', and lower numbers win."""
    if new is None:
        return False
    if old is None:
        return True
    return new < old
