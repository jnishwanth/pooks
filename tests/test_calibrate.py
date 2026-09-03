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
from pooks.rank.calibrate import (
    CategoryRatings,
    calibrate,
    category_ratings,
    summarise_categories,
)


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


# --- category baselines -------------------------------------------------------
#
# The measurement behind `[ranking.category_baselines]`. Its whole defensibility
# is that the number is observed rather than chosen, so this is the part that
# has to be right.


def _rated(store, product_id: int, categories: list[str], rating: float) -> None:
    product = Product(
        product_id=product_id,
        name=f"Book {product_id}",
        isbn=str(product_id).rjust(13, "0"),
        categories=categories,
        in_stock=True,
    )
    store.upsert_product(product)
    # Keyed off the product rather than spelled out: `book_key` is derived from
    # the ISBN, so a hand-written key silently joins to nothing and every
    # assertion that expects an empty result passes for the wrong reason.
    store.put_enrichment(
        product.book_key, {"provenance_json": "{}", "refresh_attempts": 0, "rating": rating}
    )
    store.conn.commit()


def test_category_ratings_measures_each_category_separately(store) -> None:
    for i, rating in enumerate([4.4, 4.3, 4.2], start=1):
        _rated(store, i, ["Comics"], rating)
    for i, rating in enumerate([3.8, 3.9, 4.0], start=10):
        _rated(store, i, ["Non Fiction"], rating)

    rows = {r.category: r for r in category_ratings(store, min_books=3)}

    assert rows["Comics"].mean_rating == 4.3
    assert rows["Comics"].books == 3
    assert rows["Non Fiction"].mean_rating == 3.9


def test_a_book_counts_toward_every_category_it_carries(store) -> None:
    """Because that is how the scorer reads it — a book in two categories is
    judged against whichever baseline is higher, so both must be measured."""
    for i in range(1, 4):
        _rated(store, i, ["Comics", "Literature & Fiction"], 4.4)

    rows = {r.category: r.books for r in category_ratings(store, min_books=3)}

    assert rows == {"Comics": 3, "Literature & Fiction": 3}


def test_a_category_with_too_few_books_is_not_reported(store) -> None:
    """A mean over two books is not a distribution, and publishing it invites
    someone to paste it into config as though it were."""
    _rated(store, 1, ["Poetry"], 4.9)
    _rated(store, 2, ["Poetry"], 4.9)

    assert category_ratings(store, min_books=5) == []


def test_out_of_stock_books_do_not_inform_a_baseline(store) -> None:
    """The baseline describes what is buyable, which is what gets scored."""
    _rated(store, 1, ["Comics"], 4.4)
    _rated(store, 2, ["Comics"], 4.4)
    _rated(store, 3, ["Comics"], 4.4)
    store.mark_out_of_stock([3])
    store.conn.commit()

    assert category_ratings(store, min_books=3) == []


def test_only_categories_that_differ_enough_are_suggested(store) -> None:
    """A category sitting on the global mean needs no baseline, and offering
    one invites a change that does nothing but add a knob."""
    rows = [
        CategoryRatings("Comics", 11, 4.325),
        CategoryRatings("Non Fiction", 8, 3.916),
    ]

    lines = "\n".join(summarise_categories(rows, global_mean=3.9, min_books=5))

    assert "Comics = 4.325" in lines
    assert '"Non Fiction" =' not in lines
    assert lines.count("=") == 1, "exactly one suggestion, for Comics"


def test_a_category_name_with_a_space_is_quoted_for_toml(store) -> None:
    """The output is meant to be pasted straight into config.toml, where a bare
    key containing a space is a syntax error."""
    lines = "\n".join(summarise_categories([CategoryRatings("Children's Books", 9, 4.4)], 3.9, 5))

    assert '"Children\'s Books" = 4.4' in lines
