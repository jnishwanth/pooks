"""The queries the CLI and the dashboard read the pipeline's state through.

These used to be hand-written SQL at each call site, which is how the same
count came to exist in three places. Covering them here is what makes a schema
change checkable in one file rather than by grepping for table names.
"""

from __future__ import annotations

from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
from pooks.llm.roles import Role
from pooks.models import Product

BREAKDOWN = {"score": 0.7, "confidence": 0.8}


def _stock(store: Store, products: list[Product]) -> None:
    apply(products, classify(products, store, full_sweep=True), store)


def _enrich(store: Store, book_key: str) -> None:
    store.put_enrichment(book_key, {"provenance_json": "{}", "refresh_attempts": 0})


def test_in_stock_products_narrows_to_the_unenriched(
    store: Store, products: list[Product]
) -> None:
    """`pooks enrich` without --force must skip what is already cached; with it,
    the same query has to return everything so the cache can be re-fetched."""
    _stock(store, products)
    _enrich(store, products[0].book_key)

    every = store.in_stock_products(1000)
    todo = store.in_stock_products(1000, missing_enrichment=True)

    assert len(every) == len(products)
    assert len(todo) == len(every) - 1
    assert products[0].book_key not in {row["book_key"] for row in todo}


def test_in_stock_products_excludes_sold_out(store: Store, products: list[Product]) -> None:
    _stock(store, products)
    store.mark_out_of_stock([products[0].product_id])

    assert len(store.in_stock_products(1000)) == len(products) - 1


def test_product_counts_separates_tracked_from_buyable(
    store: Store, products: list[Product]
) -> None:
    """Sold-out rows are retained deliberately — they are what makes a price
    drop across relists visible — so 'tracked' and 'in stock' diverge."""
    assert store.product_counts() == {"tracked": 0, "in_stock": 0, "scored": 0}

    _stock(store, products)
    store.mark_out_of_stock([products[0].product_id])
    store.put_score(products[1].product_id, BREAKDOWN)

    assert store.product_counts() == {
        "tracked": len(products),
        "in_stock": len(products) - 1,
        "scored": 1,
    }


def test_event_counts_by_type_is_ordered_by_frequency(
    store: Store, products: list[Product]
) -> None:
    _stock(store, products)
    store.mark_out_of_stock([products[0].product_id])
    _stock(store, products[:1])

    counts = store.event_counts_by_type()

    assert [row["n"] for row in counts] == sorted((row["n"] for row in counts), reverse=True)
    assert dict(counts[0]) == {"event_type": "NEW_IN_STOCK", "n": len(products)}


def test_get_llm_many_returns_one_entry_per_cached_key(store: Store) -> None:
    store.put_llm("isbn:1", Role.BLURB, 1, {"blurb": "one"})
    store.put_llm("isbn:2", Role.BLURB, 1, {"blurb": "two"})
    store.put_llm("isbn:2", Role.RENOWN, 1, {"tier": "known"})
    store.put_llm("isbn:3", Role.BLURB, 2, {"blurb": "newer prompt"})

    found = store.get_llm_many(["isbn:1", "isbn:2", "isbn:3"], Role.BLURB, 1)

    assert found == {"isbn:1": {"blurb": "one"}, "isbn:2": {"blurb": "two"}}
    assert store.get_llm_many([], Role.BLURB, 1) == {}


def test_scores_without_enrichment_behind_them_are_pruned(
    store: Store, products: list[Product]
) -> None:
    """A score outlives the enrichment it was computed from — after a book_key
    change, say — and would otherwise sit in `top` forever, computed under a
    scoring function nothing else in the list shares."""
    _stock(store, products)
    _enrich(store, products[0].book_key)
    store.put_score(products[0].product_id, BREAKDOWN)
    store.put_score(products[1].product_id, BREAKDOWN)

    assert store.prune_unbacked_scores() == 1
    assert store.product_counts()["scored"] == 1
