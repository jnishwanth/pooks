"""Author recovery, dashboard filters, and the price-history line."""

from __future__ import annotations

from pooks.db.store import Store
from pooks.models import Product, author_from_title, make_book_key
from pooks.serve.app import _apply_filters

# --- author recovery ----------------------------------------------------------
#
# The shop leaves `Author` unset on ~51% of in-stock listings (verified against
# the live API), but the title carries it for 90% of those.


def test_recovers_author_from_a_plain_title() -> None:
    assert author_from_title("Norwegian Wood by Haruki Murakami") == "Haruki Murakami"


def test_strips_a_trailing_edition_note() -> None:
    assert (
        author_from_title("Why I Am a Hindu by Shashi Tharoor (Hardcover)")
        == "Shashi Tharoor"
    )


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
        "confidence": 0.8,
        "rating": 4.13,
        "ratings_count": 19_192,
    }
    base.update(kw)
    return base


def _filter(books, **kw):
    defaults = {
        "q": "",
        "tag": "",
        "min_rating": 0.0,
        "min_ratings_count": 0,
        "min_confidence": 0.0,
        "unscored": True,
    }
    defaults.update(kw)
    return _apply_filters(books, **defaults)


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
    assert (
        store.previous_price_paise(products[0].book_key, products[0].product_id) is None
    )


def test_search_finds_unscored_books() -> None:
    """A search is a lookup, not a browse. Someone typing an author's name wants
    to know whether the shop stocks it, not whether the pipeline has reached it
    yet — hiding unscored books returned an empty result for books plainly in
    stock (e.g. "kahneman" during a partial backfill)."""
    books = [_book(name="Thinking, Fast and Slow", author="Daniel Kahneman",
                   score=None, confidence=0.0, rating=None, ratings_count=None)]

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
