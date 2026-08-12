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
from pooks.rank.calibrate import calibrate


def _score(store: Store, product: Product, score: float, confidence: float) -> None:
    store.put_score(product.product_id, {"score": score, "confidence": confidence})


def test_would_push_gates_on_score_confidence_and_stock(
    store: Store, products: list[Product]
) -> None:
    apply(products, classify(products, store, full_sweep=True), store)
    _score(store, products[0], 0.9, 0.9)  # passes both gates
    _score(store, products[1], 0.9, 0.1)  # good score, untrusted
    _score(store, products[2], 0.3, 0.9)  # trusted, but not good enough
    _score(store, products[3], 0.9, 0.9)  # would pass, but is sold out
    store.mark_out_of_stock([products[3].product_id])

    pushed = calibrate(store).would_push(0.5, 0.5)

    assert [book.name for book in pushed] == [products[0].name]


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


def test_no_suggestions_without_an_eligible_book(store: Store, products: list[Product]) -> None:
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


def test_would_push_includes_books_landing_exactly_on_a_gate(
    store: Store, products: list[Product]
) -> None:
    """Both gates are inclusive, and a suggested threshold always lands on one.

    `percentile` returns an observed score rather than an interpolated one, so
    every threshold `pooks calibrate` suggests is exactly the score of some
    book. An exclusive comparison would report each suggestion as pushing one
    fewer book than it actually does.
    """
    apply(products, classify(products, store, full_sweep=True), store)
    _score(store, products[0], 0.9, 0.9)  # score exactly on the threshold
    _score(store, products[1], 0.95, 0.5)  # confidence exactly on the floor
    _score(store, products[2], 0.3, 0.9)

    pushed = calibrate(store).would_push(0.9, 0.5)

    assert [book.name for book in pushed] == [products[1].name, products[0].name]


def test_would_push_lists_the_best_book_first(store: Store, products: list[Product]) -> None:
    """The CLI prints the first ten and calls them what would push right now."""
    apply(products, classify(products, store, full_sweep=True), store)
    _score(store, products[0], 0.7, 0.9)
    _score(store, products[1], 0.9, 0.9)
    _score(store, products[2], 0.8, 0.9)

    pushed = calibrate(store).would_push(0.5, 0.5)

    assert [book.score for book in pushed] == [0.9, 0.8, 0.7]
    # The listing carries what the CLI renders beside the score.
    assert [book.price_paise for book in pushed[:1]] == [products[1].price_paise]
