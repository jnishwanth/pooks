"""What each source said, and the rule for choosing between them.

The merged `enrichment` row answers "what do we believe"; these answer "who
told us, and what did everyone else say". The distinction is what makes a
repair pass able to tell "the primary source has nothing" from "the primary
source was never asked", which the merged row cannot express.
"""

from __future__ import annotations

from pooks.db.store import Store, connect
from pooks.enrich.observations import (
    Field,
    Ledger,
    Observation,
    price_observation,
    rating_observation,
    tags_observation,
)
from pooks.enrich.sources import IndianPrice, RatingResult

CHAIN = ["goodreads", "hardcover", "google_books", "open_library"]
FLOORS = {"goodreads": 50, "hardcover": 10, "google_books": 15, "open_library": 25}


def _store_rows(store: Store, book_key: str, *observations: Observation) -> Ledger:
    store.put_observations(book_key, [(o.field, o.source, o.encode()) for o in observations])
    store.conn.commit()
    return Ledger.from_rows(store.observations(book_key))


def _rating(source: str, rating: float, count: int, **kw: object) -> Observation:
    return rating_observation(RatingResult(source=source, rating=rating, ratings_count=count, **kw))


# --- storage ------------------------------------------------------------------


def test_a_source_replaces_only_its_own_answer(store: Store) -> None:
    """The property the merged row could not offer: re-asking Goodreads must
    not touch what Hardcover said, so nothing is lost by improving one field."""
    ledger = _store_rows(
        store,
        "isbn:1",
        _rating("goodreads", 4.1, 7_516),
        _rating("hardcover", 3.9, 200),
    )
    assert ledger.sources(Field.RATING) == {"goodreads", "hardcover"}

    updated = _store_rows(store, "isbn:1", _rating("goodreads", 4.2, 8_000))

    assert updated.sources(Field.RATING) == {"goodreads", "hardcover"}
    assert updated.rating_value(CHAIN, FLOORS) == (4.2, 8_000, "goodreads")
    assert updated.by_field[Field.RATING]["hardcover"]["rating"] == 3.9


def test_an_unknown_field_is_kept_rather_than_dropped(store: Store) -> None:
    """A downgrade must not silently delete a newer version's work: the ledger
    ignores what it cannot interpret instead of treating it as corrupt."""
    store.put_observations("isbn:1", [("something_newer", "future", '{"x": 1}')])
    store.conn.commit()

    ledger = Ledger.from_rows(store.observations("isbn:1"))

    assert ledger.by_field == {}
    assert store.observations("isbn:1"), "the row is still on disk"


# --- choosing between sources -------------------------------------------------


def test_the_chain_order_decides_which_rating_wins(store: Store) -> None:
    ledger = _store_rows(
        store,
        "isbn:1",
        _rating("open_library", 2.33, 300),
        _rating("goodreads", 4.11, 7_516),
    )

    assert ledger.rating_value(CHAIN, FLOORS) == (4.11, 7_516, "goodreads")
    # Reordering the chain redefines "better" without refetching anything.
    assert ledger.rating_value(["open_library", "goodreads"], FLOORS)[2] == "open_library"


def test_a_thin_sample_is_recorded_but_not_used(store: Store) -> None:
    """Open Library really does report 5.0 from 1 rating — seen on live data.
    That is a fact about the book worth keeping and a number worth never
    ranking on, which is exactly the split a stored observation allows."""
    ledger = _store_rows(
        store,
        "isbn:1",
        _rating("open_library", 5.0, 1),
        _rating("hardcover", 3.93, 14),
    )

    assert ledger.sources(Field.RATING) == {"open_library", "hardcover"}
    assert ledger.rating_value(CHAIN, FLOORS) == (3.93, 14, "hardcover")


def test_no_rating_clears_its_floor_yields_nothing(store: Store) -> None:
    ledger = _store_rows(store, "isbn:1", _rating("goodreads", 4.9, 3))

    assert ledger.rating_value(CHAIN, FLOORS) is None


def test_the_winner_supplies_the_resolved_title_and_author(store: Store) -> None:
    """The title shown must be the one the *winning* source spelled, not a
    loser's — they disagree, and the price check matches against it."""
    ledger = _store_rows(
        store,
        "isbn:1",
        _rating(
            "goodreads", 4.13, 19_195, title="Memoirs of a Dutiful Daughter", author="Beauvoir"
        ),
        _rating("open_library", 3.0, 900, title="Memoires d'une jeune fille rangee", author="X"),
    )

    assert ledger.resolved(CHAIN, FLOORS) == ("Memoirs of a Dutiful Daughter", "Beauvoir")


# --- monotonicity -------------------------------------------------------------


def test_a_worse_answer_arriving_later_cannot_displace_a_better_one(store: Store) -> None:
    """What replaced the hand-written per-field merge.

    A repair runs precisely when the last attempt was degraded, so the refetch
    can come back worse. Choosing from the whole set makes that safe by
    construction rather than by a rule someone has to get right per field.
    """
    _store_rows(store, "isbn:1", _rating("goodreads", 4.11, 7_516))

    ledger = _store_rows(store, "isbn:1", _rating("open_library", 2.33, 300))

    assert ledger.rating_value(CHAIN, FLOORS) == (4.11, 7_516, "goodreads")


def test_a_better_source_answering_later_does_take_over(store: Store) -> None:
    """The other half: the point of repairing is that an upgrade lands."""
    _store_rows(store, "isbn:1", _rating("open_library", 3.5, 300))

    ledger = _store_rows(store, "isbn:1", _rating("goodreads", 4.11, 7_516))

    assert ledger.rating_value(CHAIN, FLOORS) == (4.11, 7_516, "goodreads")


# --- tags, prices and prose ---------------------------------------------------


def test_never_asked_and_asked_with_nothing_stay_distinct(store: Store) -> None:
    """Collapsing these marks ~40% of the catalogue improvable forever and
    burns the retry budget on a lookup that can never succeed."""
    never_asked = Ledger.from_rows(store.observations("isbn:unknown"))
    assert never_asked.tags() is None

    asked = _store_rows(store, "isbn:1", tags_observation("hardcover", {}))
    assert asked.tags() == {}

    answered = _store_rows(store, "isbn:1", tags_observation("hardcover", {"genre": ["manga"]}))
    assert answered.tags() == {"genre": ["manga"]}


def test_the_cheapest_tier_with_a_real_price_wins(store: Store) -> None:
    ledger = _store_rows(
        store,
        "isbn:1",
        price_observation("bookswagon", IndianPrice(price_paise=62_900, available_in_india=True)),
        price_observation("amazon.in", IndianPrice(price_paise=33_630, available_in_india=True)),
    )

    price = ledger.price()
    assert price is not None
    assert (price.source, price.price_paise) == ("amazon.in", 33_630)


def test_a_blocked_lookup_survives_when_nothing_found_a_price(store: Store) -> None:
    """ "We were blocked" and "no Indian retailer stocks this" are both real
    answers, and scoring the first as scarcity would reward a network failure."""
    ledger = _store_rows(
        store,
        "isbn:1",
        price_observation("amazon.in", IndianPrice(available_in_india=False, unknown=True)),
    )

    price = ledger.price()
    assert price is not None and price.unknown is True and price.has_price is False


def test_the_longest_synopsis_wins(store: Store) -> None:
    """No quality ladder exists for prose, and the failure this avoids is a
    one-line Open Library stub displacing a real publisher synopsis."""
    ledger = _store_rows(
        store,
        "isbn:1",
        Observation(Field.SYNOPSIS, "open_library", {"synopsis": "A novel."}),
        Observation(Field.SYNOPSIS, "google_books", {"synopsis": "A much longer description."}),
    )

    assert ledger.synopsis(CHAIN) == "A much longer description."


# --- seeding from rows written before this table existed ----------------------


def test_a_legacy_enrichment_row_is_seeded_into_the_ledger(tmp_path) -> None:
    """`enrichment` is a projection now, so a row predating this table has to be
    seeded or the book reads as never enriched and every source is re-asked for
    what was already known.
    """
    db_path = tmp_path / "pooks.db"
    store = Store(connect(db_path))
    store.put_enrichment(
        "isbn:1",
        {
            "rating": 4.13,
            "ratings_count": 19_195,
            "rating_source": "goodreads",
            "resolved_title": "Memoirs of a Dutiful Daughter",
            "synopsis": "The first volume of her autobiography.",
            "tags_json": '{"genre": ["biography"]}',
            "comp_listing_count": 11,
            "scarcity_has_new": 1,
            "in_price_paise": 33_630,
            "in_price_source": "amazon.in",
            "in_available": 1,
            "provenance_json": "{}",
            "refresh_attempts": 0,
        },
    )
    store.conn.execute("DELETE FROM observations")  # as though it never existed
    store.conn.commit()
    store.conn.close()

    ledger = Ledger.from_rows(Store(connect(db_path)).observations("isbn:1"))

    assert ledger.rating_value(CHAIN, FLOORS) == (4.13, 19_195, "goodreads")
    assert ledger.tags() == {"genre": ["biography"]}
    assert ledger.synopsis(CHAIN) == "The first volume of her autobiography."
    assert ledger.price().source == "amazon.in"
    assert ledger.scarcity().listing_count == 11


def test_a_settled_price_with_no_recorded_source_survives_seeding(tmp_path) -> None:
    """A legacy row can say "not sold in India" without saying who found out.
    Attributing that to a real source would be a lie and dropping it would lose
    a settled answer, so it is seeded under an explicit `unattributed` and read
    back with no source at all."""
    db_path = tmp_path / "pooks.db"
    store = Store(connect(db_path))
    store.put_enrichment(
        "isbn:1",
        {"in_available": 0, "in_price_unknown": 0, "provenance_json": "{}", "refresh_attempts": 0},
    )
    store.conn.execute("DELETE FROM observations")
    store.conn.commit()
    store.conn.close()

    price = Ledger.from_rows(Store(connect(db_path)).observations("isbn:1")).price()

    assert price is not None
    assert price.available_in_india is False and price.unknown is False
    assert price.source is None, "an invented source would read as a real answer"


def test_seeding_does_not_overwrite_what_a_real_fetch_recorded(tmp_path) -> None:
    """The seed is `INSERT OR IGNORE` and probes for absence, so a source's own
    answer always wins over the merged row it was projected into."""
    db_path = tmp_path / "pooks.db"
    store = Store(connect(db_path))
    store.put_observations(
        "isbn:1", [(Field.RATING, "goodreads", '{"rating": 4.2, "ratings_count": 8000}')]
    )
    store.put_enrichment(
        "isbn:1",
        {
            "rating": 3.1,
            "ratings_count": 50,
            "rating_source": "goodreads",
            "provenance_json": "{}",
            "refresh_attempts": 0,
        },
    )
    store.conn.commit()
    store.conn.close()

    ledger = Ledger.from_rows(Store(connect(db_path)).observations("isbn:1"))

    assert ledger.rating_value(CHAIN, FLOORS) == (4.2, 8_000, "goodreads")
