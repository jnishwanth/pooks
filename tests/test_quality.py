"""Answer quality, and the rules that keep a repair pass from making things worse.

The repair pass exists because falling back used to be permanent: a rating from
Open Library was cached as durably as one from Goodreads, and a book whose price
lookup was blocked kept an empty price forever. Five of nine rows in the first
real database were frozen that way.
"""

from __future__ import annotations

from pooks.config import load_config
from pooks.db.store import Store
from pooks.enrich.pipeline import merge, persist
from pooks.enrich.quality import (
    Quality,
    assess,
    improvable,
    is_better,
    price_tier,
    rating_tier,
)
from pooks.enrich.sources import BookFacts, IndianPrice, ScarcitySignal

CHAIN = load_config().ratings["chain"]


# --- tiers --------------------------------------------------------------------


def test_rating_tier_follows_the_configured_chain() -> None:
    """Reordering [ratings].chain must redefine what counts as an upgrade,
    rather than the tiers being hardcoded somewhere else."""
    assert rating_tier("goodreads", CHAIN) == 0
    assert rating_tier("open_library", CHAIN) > rating_tier("hardcover", CHAIN)
    assert rating_tier(None, CHAIN) is None


def test_rating_tier_of_a_retired_source_is_worst_not_absent() -> None:
    """The value is real, but nothing would pick that source again."""
    assert rating_tier("some_removed_source", CHAIN) == len(CHAIN)


def test_price_tiers_rank_amazon_above_the_small_retailers() -> None:
    assert price_tier("amazon.in") == 0
    assert price_tier("bookswagon") > price_tier("amazon.in")
    assert price_tier("searxng:sapnaonline.com") > price_tier("bookswagon")
    assert price_tier(None) is None


# --- improvable ---------------------------------------------------------------


def test_blocked_price_is_improvable() -> None:
    q = Quality(rating_tier=0, price_tier=None, price_unknown=True, degraded=False)
    can, why = improvable(q, price_available=False)
    assert can and "blocked" in why


def test_not_sold_in_india_is_a_complete_answer() -> None:
    """Retrying it is pure waste — the absence was established, not guessed."""
    q = Quality(rating_tier=0, price_tier=None, price_unknown=False, degraded=False)
    can, _ = improvable(q, price_available=False)
    assert can is False


def test_fallback_rating_is_improvable() -> None:
    q = Quality(
        rating_tier=rating_tier("open_library", CHAIN),
        price_tier=0,
        price_unknown=False,
        degraded=False,
    )
    can, why = improvable(q, price_available=True)
    assert can and "fallback" in why


def test_fully_primary_record_is_not_improvable() -> None:
    q = Quality(rating_tier=0, price_tier=0, price_unknown=False, degraded=False)
    assert improvable(q, price_available=True)[0] is False


def test_is_better_treats_absent_as_worst() -> None:
    assert is_better(0, 2) is True
    assert is_better(2, 0) is False
    assert is_better(1, None) is True
    assert is_better(None, 1) is False


# --- monotonic merge ----------------------------------------------------------
#
# The subtle risk. A refresh runs precisely when the last attempt was degraded,
# so the source may still be throttled and the refetch can come back *worse*.
# Writing that blindly would downgrade the record and re-mark it improvable —
# a book could oscillate between tiers and be re-fetched forever.


def _good() -> BookFacts:
    return BookFacts(
        book_key="isbn:1",
        rating=4.13,
        ratings_count=19_192,
        rating_source="goodreads",
        synopsis="a synopsis",
        indian_price=IndianPrice(
            price_paise=33_630, source="amazon.in", available_in_india=True
        ),
        scarcity=ScarcitySignal(listing_count=11, has_new_offers=True),
        in_print=True,
    )


def test_a_worse_refetch_never_downgrades_the_record() -> None:
    worse = BookFacts(
        book_key="isbn:1",
        rating=3.6,
        ratings_count=90,
        rating_source="open_library",
        indian_price=IndianPrice(available_in_india=False, unknown=True),
    )

    merged = merge(_good(), worse, CHAIN)

    assert merged.rating_source == "goodreads"
    assert merged.rating == 4.13
    assert merged.indian_price.source == "amazon.in"
    assert merged.indian_price.price_paise == 33_630


def test_a_better_refetch_is_taken() -> None:
    stale = BookFacts(
        book_key="isbn:1",
        rating=3.6,
        ratings_count=90,
        rating_source="open_library",
        indian_price=IndianPrice(available_in_india=False, unknown=True),
    )

    merged = merge(stale, _good(), CHAIN)

    assert merged.rating_source == "goodreads"
    assert merged.indian_price.source == "amazon.in"


def test_halves_improve_independently() -> None:
    """A refresh can recover the price while Goodreads is still blocked."""
    old = BookFacts(
        book_key="isbn:1",
        rating=4.13,
        ratings_count=19_192,
        rating_source="goodreads",
        indian_price=IndianPrice(available_in_india=False, unknown=True),
    )
    new = BookFacts(
        book_key="isbn:1",
        rating=3.6,
        ratings_count=90,
        rating_source="open_library",
        indian_price=IndianPrice(price_paise=40_000, source="amazon.in", available_in_india=True),
    )

    merged = merge(old, new, CHAIN)

    assert merged.rating_source == "goodreads"          # kept the better rating
    assert merged.indian_price.source == "amazon.in"    # took the recovered price


def test_a_synopsis_is_never_dropped_for_an_empty_refetch() -> None:
    new = BookFacts(book_key="isbn:1", rating=4.2, ratings_count=500, rating_source="goodreads")
    merged = merge(_good(), new, CHAIN)
    assert merged.synopsis == "a synopsis"


# --- selection and the retry cap ----------------------------------------------


def _seed(store: Store, book_key: str, **overrides) -> None:
    data = {
        "isbn": book_key.removeprefix("isbn:"),
        "rating": 4.0,
        "ratings_count": 500,
        "rating_source": "goodreads",
        "provenance_json": "{}",
        "in_price_paise": 30_000,
        "in_price_source": "amazon.in",
        "in_available": 1,
        "in_price_unknown": 0,
        "refresh_attempts": 0,
    }
    data.update(overrides)
    store.put_enrichment(book_key, data)


def test_assess_reads_the_stored_row(store: Store) -> None:
    _seed(store, "isbn:1", rating_source="open_library", in_price_source="bookswagon")
    row = store.get_enrichment("isbn:1")

    q = assess(row, CHAIN, {})

    assert q.rating_tier == rating_tier("open_library", CHAIN)
    assert q.price_tier == price_tier("bookswagon")
    assert improvable(q, price_available=True)[0] is True


def test_expiry_is_set_for_a_blocked_price(store: Store) -> None:
    """The regression that motivated all of this: such a row used to be cached
    with expires_at NULL and never revisited."""
    facts = BookFacts(
        book_key="isbn:9",
        rating=4.1,
        ratings_count=7516,
        rating_source="goodreads",
        indian_price=IndianPrice(available_in_india=False, unknown=True),
    )
    persist(store, facts, chain=CHAIN)

    assert store.get_enrichment("isbn:9")["expires_at"] is not None


def test_selection_skips_books_past_the_retry_cap(store: Store, products) -> None:
    """A book nobody has ever rated must stop consuming third-party traffic."""
    from pooks.ingest.diff import apply, classify

    apply(products, classify(products, store, full_sweep=True), store)
    keys = [p.book_key for p in products]

    _seed(store, keys[0], in_price_unknown=1, in_price_source=None,
          in_price_paise=None, refresh_attempts=4)
    _seed(store, keys[1], in_price_unknown=1, in_price_source=None,
          in_price_paise=None, refresh_attempts=5)

    selected = {r["book_key"] for r in store.improvable_books(limit=10)}

    assert keys[0] in selected, "4 attempts is still under the cap"
    assert keys[1] not in selected, "5 attempts is exhausted"


def test_selection_prefers_blocked_prices_then_ranks_by_score(store: Store, products) -> None:
    from pooks.ingest.diff import apply, classify

    apply(products, classify(products, store, full_sweep=True), store)
    keys = [p.book_key for p in products]

    # A merely-second-best price, on a highly ranked book...
    _seed(store, keys[0], in_price_source="bookswagon")
    store.put_score(products[0].product_id, {"score": 0.9, "confidence": 0.8})
    # ...must still lose to an outright hole on a lower-ranked one.
    _seed(store, keys[1], in_price_unknown=1, in_price_source=None, in_price_paise=None)
    store.put_score(products[1].product_id, {"score": 0.2, "confidence": 0.8})

    order = [r["book_key"] for r in store.improvable_books(limit=10)]

    assert order.index(keys[1]) < order.index(keys[0])


def test_selection_ignores_out_of_stock_books(store: Store, products) -> None:
    """An unbuyable book cannot reach the digest, so upgrading it is traffic
    spent for nothing."""
    from pooks.ingest.diff import apply, classify

    apply(products, classify(products, store, full_sweep=True), store)
    _seed(store, products[0].book_key, in_price_unknown=1,
          in_price_source=None, in_price_paise=None)
    store.mark_out_of_stock([products[0].product_id])

    assert products[0].book_key not in {r["book_key"] for r in store.improvable_books(limit=10)}


def test_refresh_floor_skips_books_that_cannot_be_pushed(store: Store, products) -> None:
    """The one real throughput lever: a 90s Amazon lookup on a book scoring 0.2
    is spent on something that can never clear the push threshold."""
    from pooks.ingest.diff import apply, classify

    apply(products, classify(products, store, full_sweep=True), store)
    keys = [p.book_key for p in products]

    _seed(store, keys[0], in_price_unknown=1, in_price_source=None, in_price_paise=None)
    store.put_score(products[0].product_id, {"score": 0.20, "confidence": 0.8})
    _seed(store, keys[1], in_price_unknown=1, in_price_source=None, in_price_paise=None)
    store.put_score(products[1].product_id, {"score": 0.70, "confidence": 0.8})

    selected = {r["book_key"] for r in store.improvable_books(limit=10, min_score=0.55)}

    assert keys[1] in selected
    assert keys[0] not in selected, "below the floor, so not worth an expensive lookup"


def test_unscored_books_still_get_the_benefit_of_the_doubt(store: Store, products) -> None:
    """An unscored book has not been through the pipeline yet — excluding it
    would strand anything the backfill has not reached."""
    from pooks.ingest.diff import apply, classify

    apply(products, classify(products, store, full_sweep=True), store)
    _seed(store, products[0].book_key, in_price_unknown=1,
          in_price_source=None, in_price_paise=None)

    selected = {r["book_key"] for r in store.improvable_books(limit=10, min_score=0.55)}
    assert products[0].book_key in selected


def test_orphaned_enrichment_is_pruned(store: Store, products) -> None:
    """Recovering an author changes book_key for ISBN-less books, stranding the
    old enrichment row."""
    from pooks.ingest.diff import apply, classify

    apply(products, classify(products, store, full_sweep=True), store)
    _seed(store, products[0].book_key)
    _seed(store, "ta:stale-key|nobody")

    removed = store.prune_orphaned_enrichment()

    assert removed == 1
    assert store.get_enrichment(products[0].book_key) is not None
    assert store.get_enrichment("ta:stale-key|nobody") is None


def test_never_asked_for_tags_is_improvable_but_asked_and_empty_is_not(store: Store) -> None:
    """The distinction that stops ~2 books in 5 being retried forever: Hardcover
    genuinely has no tags for them, which is a settled fact, not a gap."""
    _seed(store, "isbn:never", tags_json=None)
    _seed(store, "isbn:empty", tags_json="{}")

    never = assess(store.get_enrichment("isbn:never"), CHAIN, {})
    empty = assess(store.get_enrichment("isbn:empty"), CHAIN, {})

    assert never.tags_unasked is True
    assert improvable(never, price_available=True) == (True, "tags never fetched")
    assert empty.tags_unasked is False
    assert improvable(empty, price_available=True)[0] is False


async def test_a_book_without_an_isbn_is_not_pending_tags() -> None:
    """Hardcover is looked up by ISBN. With none there is nothing to ask, so it
    is settled rather than pending — otherwise the repair pass retries a lookup
    that can never be made, forever."""
    from pooks.config import load_config
    from pooks.enrich.pipeline import Enricher

    tags = await Enricher(load_config())._fetch_tags(client=None, isbn=None)
    assert tags == {}, "must be 'asked and none', not 'never asked'"
