"""Cached enrichment must score identically to fresh enrichment.

Regression: `landed_used_min_paise` was computed during enrichment but never
persisted, so anything read from cache silently lost the price comparison and
fell back to the out-of-print branch. Fresh and cached runs of the same book
produced different scores, and `rescore` quietly degraded every book it touched.

The landed-cost field is gone now (AbeBooks no longer sets the baseline), but
the failure mode it represents is generic: any value computed during enrichment
and not round-tripped degrades cached reads while fresh runs look correct. These
tests cover the fields that replaced it.
"""

from __future__ import annotations

import pytest

from pooks.config import load_config
from pooks.db.store import Store
from pooks.enrich.pipeline import facts_from_row, persist
from pooks.enrich.sources import BookFacts, IndianPrice, ScarcitySignal
from pooks.llm.pipeline import BookInsights
from pooks.models import Product
from pooks.rank.score import score_book

CHAIN = load_config().ratings["chain"]


@pytest.fixture
def facts() -> BookFacts:
    return BookFacts(
        book_key="isbn:9780140020304",
        isbn="9780140020304",
        resolved_title="Memoirs of a Dutiful Daughter",
        resolved_author="Simone de Beauvoir",
        rating=4.13,
        ratings_count=19_192,
        rating_source="goodreads",
        synopsis="The first volume of her autobiography.",
        in_print=True,
        scarcity=ScarcitySignal(listing_count=11, has_new_offers=True),
        indian_price=IndianPrice(
            price_paise=33_630,
            source="amazon.in",
            available_in_india=True,
        ),
    )


@pytest.fixture
def product() -> Product:
    return Product(
        product_id=233180,
        name="Memoirs of a Dutiful Daughter by Simone de Beauvoir",
        isbn="9780140020304",
        author="Simone de Beauvoir",
        condition="Good",
        price_paise=22_000,
        in_stock=True,
    )


def test_indian_price_survives_the_cache_round_trip(store: Store, facts: BookFacts) -> None:
    persist(store, facts, chain=CHAIN)

    restored = facts_from_row(facts.book_key, store.get_enrichment(facts.book_key))

    assert restored.indian_price is not None
    assert restored.indian_price.price_paise == 33_630
    assert restored.indian_price.source == "amazon.in"
    assert restored.indian_price.available_in_india is True


def test_scarcity_survives_the_cache_round_trip(store: Store, facts: BookFacts) -> None:
    persist(store, facts, chain=CHAIN)

    restored = facts_from_row(facts.book_key, store.get_enrichment(facts.book_key))

    assert restored.scarcity is not None
    assert restored.scarcity.listing_count == 11
    assert restored.scarcity.has_new_offers is True


def test_cached_and_fresh_facts_score_identically(
    store: Store, facts: BookFacts, product: Product
) -> None:
    config = load_config()
    insights = BookInsights(renown_abstained=True)

    fresh = score_book(product, facts, insights, config)

    persist(store, facts, chain=CHAIN)
    restored = facts_from_row(facts.book_key, store.get_enrichment(facts.book_key))
    cached = score_book(product, restored, insights, config)

    assert cached.score == fresh.score
    assert cached.value == fresh.value
    assert cached.confidence == fresh.confidence


def test_unknown_and_unavailable_stay_distinct_across_the_cache(
    store: Store, facts: BookFacts
) -> None:
    """These two collapse into each other easily — both leave
    available_in_india False — and conflating them scores a blocked lookup as
    scarcity."""
    facts.indian_price = IndianPrice(available_in_india=False, unknown=True)
    persist(store, facts, chain=CHAIN)
    blocked = facts_from_row(facts.book_key, store.get_enrichment(facts.book_key))

    facts.indian_price = IndianPrice(available_in_india=False, unknown=False)
    persist(store, facts, chain=CHAIN)
    genuinely_absent = facts_from_row(facts.book_key, store.get_enrichment(facts.book_key))

    assert blocked.indian_price.unknown is True
    assert genuinely_absent.indian_price.unknown is False


def test_book_with_no_price_data_round_trips_as_none(store: Store, facts: BookFacts) -> None:
    facts.indian_price = None
    facts.scarcity = None
    persist(store, facts, chain=CHAIN)

    restored = facts_from_row(facts.book_key, store.get_enrichment(facts.book_key))

    assert restored.indian_price is None
    assert restored.scarcity is None
