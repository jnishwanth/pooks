"""Author recovery, dashboard filters, and the price-history line."""

from __future__ import annotations

from pooks.db.store import Store
from pooks.models import Product, author_from_title, make_book_key
from pooks.serve.app import Filters, _apply_filters

# --- author recovery ----------------------------------------------------------
#
# The shop leaves `Author` unset on ~51% of in-stock listings (verified against
# the live API), but the title carries it for 90% of those.


def test_recovers_author_from_a_plain_title() -> None:
    assert author_from_title("Norwegian Wood by Haruki Murakami") == "Haruki Murakami"


def test_strips_a_trailing_edition_note() -> None:
    assert author_from_title("Why I Am a Hindu by Shashi Tharoor (Hardcover)") == "Shashi Tharoor"


def test_titles_without_an_author_yield_nothing() -> None:
    """Real examples from stock — these must fall through to enrichment."""
    for title in (
        "Sapiens: A Brief History of Humankind",
        "The Immortals of Meluha (Shiva Trilogy)",
        "Homo Deus: A Brief History of Tomorrow",
    ):
        assert author_from_title(title) is None


def test_a_title_that_merely_contains_by_is_not_an_author() -> None:
    assert author_from_title("Stand by Me") is None


def test_attribute_wins_over_the_title() -> None:
    payload = {
        "id": 1,
        "name": "Some Book by Wrong Name",
        "attributes": [
            {"name": "Author", "terms": [{"name": "Correct Name"}]},
            {"name": "ISBN", "terms": [{"name": "9780140020304"}]},
        ],
        "prices": {"price": "22000"},
        "is_in_stock": True,
    }
    assert Product.from_store_api(payload).author == "Correct Name"


def test_recovery_does_not_disturb_the_key_of_an_isbn_book() -> None:
    """book_key churn is the risk: for ISBN-less books the key is derived from
    title and author, so a recovered author changes it and strands the cached
    enrichment. Books with an ISBN must be unaffected."""
    before = make_book_key("9780140020304", "Some Book", None)
    after = make_book_key("9780140020304", "Some Book by Someone", "Someone")
    assert before == after == "isbn:9780140020304"


# --- dashboard filters --------------------------------------------------------


def _book(**kw):
    base = {
        "name": "Memoirs of a Dutiful Daughter",
        "author": "Simone de Beauvoir",
        "score": 0.68,
        "tags": [],
        "tag_facets": {},
        "categories": [],
        "added": None,
        "confidence": 0.8,
        "rating": 4.13,
        "ratings_count": 19_192,
        "price_inr": 220.0,
    }
    base.update(kw)
    return base


def _filter(books, *, tag="", **kw):
    """`tag` stays a single string here because these tests read better that
    way; the endpoint takes it repeatably and `Filters` holds a tuple."""
    kw.setdefault("unscored", True)
    kw.setdefault("tags", (tag,) if tag else ())
    return _apply_filters(books, Filters(**kw))


def test_search_tolerates_a_misspelling() -> None:
    """The reason it uses rapidfuzz rather than SQL LIKE."""
    found = _filter([_book()], q="beauvior")
    assert len(found) == 1


def test_search_matches_a_partial_title() -> None:
    assert len(_filter([_book()], q="dutiful")) == 1


def test_search_excludes_unrelated_books() -> None:
    books = [_book(), _book(name="Naruto 30", author="Masashi Kishimoto")]
    found = _filter(books, q="beauvoir")
    assert [b["name"] for b in found] == ["Memoirs of a Dutiful Daughter"]


def test_min_ratings_count_excludes_thin_samples() -> None:
    """Pairs with the Bayesian shrinkage: a 4.9 from 12 ratings is noise."""
    books = [_book(), _book(name="Obscure", rating=4.9, ratings_count=12)]
    found = _filter(books, min_ratings_count=1000)
    assert [b["name"] for b in found] == ["Memoirs of a Dutiful Daughter"]


def test_min_rating_filters_on_the_star_value() -> None:
    books = [_book(), _book(name="Mediocre", rating=3.1)]
    assert len(_filter(books, min_rating=4.0)) == 1


def test_filters_compose() -> None:
    books = [
        _book(),
        _book(name="Thin", rating=4.9, ratings_count=5),
        _book(name="Naruto 30", author="Masashi Kishimoto", ratings_count=50_000),
    ]
    found = _filter(books, q="beauvoir", min_ratings_count=1000, min_rating=4.0)
    assert [b["name"] for b in found] == ["Memoirs of a Dutiful Daughter"]


def test_unscored_books_are_hidden_by_default() -> None:
    books = [_book(), _book(name="Unscored", score=None)]
    assert len(_filter(books, unscored=False)) == 1


# --- price history ------------------------------------------------------------


def test_previous_price_looks_across_listings(store: Store, products) -> None:
    """Relists get a new product id, so a same-product PRICE_CHANGE almost never
    fires — the comparison has to span listings of the same book."""
    original = products[0]
    store.upsert_product(original)

    relisted = original.model_copy(
        update={"product_id": original.product_id + 500_000, "price_paise": 15_000}
    )
    store.upsert_product(relisted)

    previous = store.previous_price_paise(relisted.book_key, relisted.product_id)
    assert previous == original.price_paise


def test_previous_price_ignores_the_listing_itself(store: Store, products) -> None:
    store.upsert_product(products[0])
    assert store.previous_price_paise(products[0].book_key, products[0].product_id) is None


def test_search_finds_unscored_books() -> None:
    """A search is a lookup, not a browse. Someone typing an author's name wants
    to know whether the shop stocks it, not whether the pipeline has reached it
    yet — hiding unscored books returned an empty result for books plainly in
    stock (e.g. "kahneman" during a partial backfill)."""
    books = [
        _book(
            name="Thinking, Fast and Slow",
            author="Daniel Kahneman",
            score=None,
            confidence=0.0,
            rating=None,
            ratings_count=None,
        )
    ]

    assert len(_filter(books, q="kahneman", unscored=False, min_confidence=0.5)) == 1
    # ...but browsing without a query still respects the filters.
    assert len(_filter(books, unscored=False, min_confidence=0.5)) == 0


# --- Hardcover tags -----------------------------------------------------------


def test_tag_filter_matches_a_slug() -> None:
    books = [
        _book(name="Naruto 30", tags=["fantasy", "manga", "tense"]),
        _book(name="Memoirs of a Dutiful Daughter", tags=["classics", "biography"]),
    ]
    found = _filter(books, tag="manga")
    assert [b["name"] for b in found] == ["Naruto 30"]


def test_tag_filter_composes_with_the_others() -> None:
    books = [
        _book(name="Naruto 30", tags=["fantasy"], ratings_count=8_574),
        _book(name="Obscure Fantasy", tags=["fantasy"], ratings_count=12),
    ]
    found = _filter(books, tag="fantasy", min_ratings_count=1000)
    assert [b["name"] for b in found] == ["Naruto 30"]


def test_untagged_books_are_excluded_by_a_tag_filter() -> None:
    """Hardcover has nothing for ~2 books in 5, so a tag filter necessarily
    hides them — worth pinning so the behaviour is deliberate."""
    assert _filter([_book(tags=[])], tag="classics") == []


# --- tag and category filtering ----------------------------------------------


def test_excluding_a_tag_hides_the_books_carrying_it() -> None:
    """The browse-side answer to "too much manga at the top": the score stays
    objective and the reader narrows what they are shown."""
    books = [_book(name="Naruto 30", tags=["manga", "fantasy"]), _book(name="Memoirs")]

    found = _filter(books, exclude_tags=("manga",))

    assert [b["name"] for b in found] == ["Memoirs"]


def test_exclusion_beats_inclusion_for_the_same_book() -> None:
    """A book matching both an included and an excluded tag is excluded — the
    negative filter is the more specific statement of intent."""
    books = [_book(name="Naruto 30", tags=["manga", "fantasy"])]

    assert _filter(books, tag="fantasy", exclude_tags=("manga",)) == []


def test_any_mode_widens_and_all_mode_narrows() -> None:
    books = [
        _book(name="Both", tags=["manga", "tense"]),
        _book(name="One", tags=["manga"]),
    ]

    either = _filter(books, tags=("manga", "tense"), tag_mode="any")
    both = _filter(books, tags=("manga", "tense"), tag_mode="all")

    assert {b["name"] for b in either} == {"Both", "One"}
    assert [b["name"] for b in both] == ["Both"]


def test_category_filtering_is_case_insensitive() -> None:
    """Categories are display strings from the shop, not slugs, so a link built
    from one must still match the stored casing."""
    books = [_book(name="Naruto 30", categories=["Comics"]), _book(name="Memoirs")]

    assert [b["name"] for b in _filter(books, categories=("comics",))] == ["Naruto 30"]
    assert [b["name"] for b in _filter(books, exclude_categories=("COMICS",))] == ["Memoirs"]


def test_categories_cover_books_that_have_no_tags() -> None:
    """Why category filtering exists alongside tags: Hardcover reaches about
    three books in five, the shop's categories reach all of them."""
    untagged = _book(name="Naruto 30", tags=[], categories=["Comics"])

    assert _filter([untagged], exclude_tags=("manga",)) == [untagged]
    assert _filter([untagged], exclude_categories=("Comics",)) == []


# --- sorting and the arrival window -------------------------------------------


def test_sorts_order_by_what_they_name() -> None:
    books = [
        _book(name="cheap", score=0.1, price_inr=100.0, rating=3.0, ratings_count=5),
        _book(name="dear", score=0.9, price_inr=900.0, rating=4.9, ratings_count=9_000),
    ]

    assert [b["name"] for b in _filter(books, sort="score")] == ["dear", "cheap"]
    assert [b["name"] for b in _filter(books, sort="price")] == ["cheap", "dear"]
    assert [b["name"] for b in _filter(books, sort="rating")] == ["dear", "cheap"]
    assert [b["name"] for b in _filter(books, sort="ratings_count")] == ["dear", "cheap"]


def test_an_unknown_sort_falls_back_to_score() -> None:
    """`sort` comes off the query string, so a stale bookmark must not 500 or
    return the list in whatever order it happened to be built in."""
    books = [_book(name="low", score=0.1), _book(name="high", score=0.9)]

    assert [b["name"] for b in _filter(books, sort="nonsense")] == ["high", "low"]


def test_added_sorts_a_mix_of_naive_and_aware_stamps() -> None:
    """The live database holds both shapes at once, mid-backfill.

    `date_created` arrives from wp/v2 naive, `first_seen_at` is written with an
    offset, and Python refuses to compare the two — so sorting the catalogue
    while the sweep is still filling dates raises `TypeError` unless the naive
    ones are read as UTC first.
    """
    books = [
        _book(name="from wp/v2", added="2026-08-01T09:00:00"),
        _book(name="first seen", added="2026-08-09T09:00:00+00:00"),
    ]

    assert [b["name"] for b in _filter(books, sort="added")] == ["first seen", "from wp/v2"]


def test_added_sorts_by_instant_rather_than_by_string() -> None:
    """An offset makes the two orderings disagree: 23:00+05:30 is *earlier*
    than 18:00Z, but sorts after it as text. `date_created` comes from an
    external API, so its offset is that site's choice rather than ours."""
    books = [
        _book(name="earlier", added="2026-08-09T23:00:00+05:30"),  # 17:30Z
        _book(name="later", added="2026-08-09T18:00:00+00:00"),  # 18:00Z
    ]

    assert [b["name"] for b in _filter(books, sort="added")] == ["later", "earlier"]


def test_the_arrival_window_excludes_books_with_no_known_date() -> None:
    """The window answers "what is new", and an unknown date is not evidence of
    being recent — the whole catalogue had a NULL `date_created` until the
    sweep started filling it."""
    from datetime import UTC, datetime, timedelta

    recent = datetime.now(UTC) - timedelta(days=2)
    books = [
        _book(name="recent", added=recent.isoformat()),
        _book(name="ancient", added="2020-01-01T00:00:00+00:00"),
        _book(name="unknown", added=None),
    ]

    assert [b["name"] for b in _filter(books, added_within_days=7)] == ["recent"]
