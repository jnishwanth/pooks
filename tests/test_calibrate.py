"""Turning the push thresholds into a measured choice.

The subtle part is that the suggested thresholds are computed over *eligible*
books only. A book below the confidence floor can never be pushed whatever the
score threshold is, so counting it would drag every percentile toward scores no
setting can actually reach.
"""

from __future__ import annotations

from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
from pooks.models import Product
from pooks.rank.calibrate import calibrate, would_notify


def _score(store: Store, product: Product, score: float, confidence: float) -> None:
    store.put_score(product.product_id, {"score": score, "confidence": confidence})


def test_would_notify_gates_on_score_confidence_and_stock(
    store: Store, products: list[Product]
) -> None:
    apply(products, classify(products, store, full_sweep=True), store)
    _score(store, products[0], 0.9, 0.9)  # passes both gates
    _score(store, products[1], 0.9, 0.1)  # good score, untrusted
    _score(store, products[2], 0.3, 0.9)  # trusted, but not good enough
    _score(store, products[3], 0.9, 0.9)  # would pass, but is sold out
    store.mark_out_of_stock([products[3].product_id])

    pushed = would_notify(store, 0.5, 0.5)

    assert [book["name"] for book in pushed] == [products[0].name]


def test_suggestions_are_drawn_only_from_pushable_books(
    store: Store, products: list[Product]
) -> None:
    apply(products, classify(products, store, full_sweep=True), store)
    for product in products[:5]:
        _score(store, product, 0.1, 0.1)
    _score(store, products[5], 0.8, 0.9)

    result = calibrate(store, min_confidence=0.5)

    # Every percentile is 0.8: the five low-confidence books are in the reported
    # distribution but cannot inform a threshold they can never satisfy.
    assert result.scored == len(products)
    assert set(result.suggestions.values()) == {0.8}


def test_no_suggestions_without_an_eligible_book(
    store: Store, products: list[Product]
) -> None:
    apply(products, classify(products, store, full_sweep=True), store)
    _score(store, products[0], 0.9, 0.1)

    result = calibrate(store, min_confidence=0.5)

    assert result.suggestions == {}
    assert not result.enough_data


def test_unscored_books_count_toward_the_in_stock_total(
    store: Store, products: list[Product]
) -> None:
    """`scored / in_stock` has to compare against the catalogue, not itself.

    Selecting only rows that have a score made both halves the same number, so
    the headline read `8 / 8` on a shop with 633 in-stock books.
    """
    apply(products, classify(products, store, full_sweep=True), store)
    _score(store, products[0], 0.9, 0.9)
    store.mark_out_of_stock([products[1].product_id])

    result = calibrate(store)

    assert result.scored == 1
    assert result.in_stock == len(products) - 1


def test_would_push_counts_the_same_books_would_notify_lists(
    store: Store, products: list[Product]
) -> None:
    """The count in the summary and the list the CLI prints must agree —
    they were two independent readings of the same rule."""
    apply(products, classify(products, store, full_sweep=True), store)
    _score(store, products[0], 0.9, 0.9)
    _score(store, products[1], 0.9, 0.1)
    _score(store, products[2], 0.3, 0.9)
    _score(store, products[3], 0.9, 0.5)  # confidence exactly on the floor

    result = calibrate(store)

    # 0.9 sits exactly on a score, so both gates are inclusive-boundary tested.
    for threshold in (0.0, 0.5, 0.9, 0.95):
        assert result.would_push(threshold, 0.5) == len(
            would_notify(store, threshold, 0.5)
        )
