"""Rebuilding a book from cache.

`pooks blurbs`, `pooks notify` and `rescore` all work entirely from stored rows
rather than re-fetching, so `load_cached` is the single place that knows how a
product, its enrichment and its cached inference fit back together. A book that
is only half present in the cache must be skipped rather than half rebuilt: the
scorer treats a missing rating as a real absence, so a book reassembled without
its enrichment would score as though every source had answered "nothing".
"""

from __future__ import annotations

from pooks.config import load_config
from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
from pooks.llm.roles import Role
from pooks.models import Product
from pooks.run import load_cached, ranked_cached


def _stock(store: Store, products: list[Product]) -> None:
    apply(products, classify(products, store, full_sweep=True), store)


def _enrich(store: Store, product: Product) -> None:
    store.put_enrichment(
        product.book_key,
        {"rating": 4.2, "rating_source": "goodreads", "provenance_json": "{}",
         "refresh_attempts": 0},
    )


def _score(store: Store, product: Product, score: float) -> None:
    store.put_score(product.product_id, {"score": score, "confidence": 0.8})


def test_load_cached_skips_a_book_with_no_enrichment(
    store: Store, products: list[Product]
) -> None:
    config = load_config()
    _stock(store, products)
    _enrich(store, products[0])

    rows = {row["product_id"]: row for row in store.ranked_in_stock()}

    assert load_cached(store, config, rows[products[0].product_id]) is not None
    assert load_cached(store, config, rows[products[1].product_id]) is None


def test_load_cached_needs_both_roles_before_it_trusts_the_llm_cache(
    store: Store, products: list[Product]
) -> None:
    """A blurb without a renown judgement is not half an answer — renown feeds
    the score, so filling it with a cached-looking default would rank the book
    as though the model had abstained rather than never been asked."""
    config = load_config()
    product = products[0]
    _stock(store, products)
    _enrich(store, product)
    store.put_llm(product.book_key, Role.BLURB, config.prompt_version, {"blurb": "a note"})

    row = store.get_product(product.product_id)
    assert load_cached(store, config, row)[2].blurb is None

    store.put_llm(
        product.book_key,
        Role.RENOWN,
        config.prompt_version,
        {"tier": "major", "score": 0.7, "abstained": False},
    )
    assert load_cached(store, config, row)[2].blurb == "a note"


def test_ranked_cached_yields_scored_enriched_books_best_first(
    store: Store, products: list[Product]
) -> None:
    """The set both `blurbs` and `notify` want: unscored books have not been
    through the pipeline, and un-enriched ones cannot be rebuilt at all."""
    config = load_config()
    _stock(store, products)
    for product in products[:3]:
        _enrich(store, product)
    _score(store, products[0], 0.4)
    _score(store, products[1], 0.9)
    _score(store, products[3], 0.8)  # scored but never enriched

    found = list(ranked_cached(store, config))

    assert [product.product_id for _, product, _, _ in found] == [
        products[1].product_id,
        products[0].product_id,
    ]
    assert [row["score"] for row, *_ in found] == [0.9, 0.4]


def test_ranked_cached_limit_applies_before_the_skips(
    store: Store, products: list[Product]
) -> None:
    """`limit` bounds the ranking read, not the yield — `pooks blurbs --top 25`
    means "the top 25 books", so a second run is a no-op rather than quietly
    walking deeper into the ranking to make up the number."""
    config = load_config()
    _stock(store, products)
    for product in products[:2]:
        _enrich(store, product)
    _score(store, products[0], 0.9)
    _score(store, products[1], 0.1)

    assert len(list(ranked_cached(store, config, limit=1))) == 1
    assert len(list(ranked_cached(store, config, limit=2))) == 2
