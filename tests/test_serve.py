"""The dashboard's HTTP surface.

Exercised through the app rather than through `_apply_filters` because the
defect these tests exist for lived entirely in the query-parameter layer, which
a direct call to the filter function cannot reach: every search submitted from
the form was answered with 422, and the filtering code it never reached was
perfectly correct.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from pooks.config import load_config
from pooks.db.store import Store, connect
from pooks.models import Product
from pooks.serve import app as serve_app

# What a browser actually sends. The form renders `value=""` for a numeric
# filter that is not set, so these blanks ride along with every single search.
FORM_BLANKS = {"min_rating": "", "min_ratings_count": "", "min_confidence": "0.0"}


@pytest.fixture
def client(tmp_path, products: list[Product], monkeypatch) -> TestClient:
    """A dashboard over a temporary database.

    A file rather than the shared in-memory `store` fixture, and a fresh
    connection per `_open` call rather than one handed in: that is what the app
    does in production — `_open` runs inside the request — and sqlite3 refuses
    to let one connection cross threads, which is exactly where TestClient runs
    the handler.
    """
    db_path = tmp_path / "pooks.db"
    setup = Store(connect(db_path))
    for product in products:
        setup.upsert_product(product)
    setup.conn.commit()
    setup.conn.close()

    config = load_config()
    monkeypatch.setattr(serve_app, "_open", lambda: (config, Store(connect(db_path))))
    return TestClient(serve_app.app)


def test_a_search_from_the_form_is_not_rejected_as_a_number(client: TestClient) -> None:
    """The reported symptom: searching looked like it wanted a number, not text.

    422 `{"type":"float_parsing","loc":["query","min_rating"],"msg":"Input
    should be a valid number"}` — the empty *filters* failed to parse and took
    the whole request down with them.
    """
    response = client.get("/", params={**FORM_BLANKS, "q": "beauvoir"})

    assert response.status_code == 200
    assert "Memoirs of a Dutiful Daughter" in response.text


def test_a_misspelled_search_still_reaches_the_fuzzy_match(client: TestClient) -> None:
    """The whole reason `q` goes through rapidfuzz rather than SQL LIKE — and
    unreachable while the request was rejected before any filtering ran."""
    response = client.get("/", params={**FORM_BLANKS, "q": "beauvior"})

    assert response.status_code == 200
    assert "Memoirs of a Dutiful Daughter" in response.text


def test_blank_filters_do_not_filter(client: TestClient) -> None:
    """A blank box means "no filter", not "reject the request" and not zero
    results — `?min_rating=` must behave exactly like omitting it."""
    blank = client.get("/api/books", params={**FORM_BLANKS, "limit": ""})
    absent = client.get("/api/books")

    assert blank.status_code == absent.status_code == 200
    assert [b["product_id"] for b in blank.json()] == [b["product_id"] for b in absent.json()]
    assert blank.json(), "the fixture catalogue should not come back empty"


def test_a_blank_checkbox_means_off_not_a_rejection(client: TestClient) -> None:
    """`unscored` was the last parameter on `/` that still 422'd on a blank.

    Unreachable from the form — a browser omits an unticked checkbox entirely —
    but `?unscored=` is hand-typeable, which is the same standard the numeric
    filters were fixed to. Nothing in the fixture catalogue is scored, so
    "showing unscored books" is directly observable.
    """
    blank = client.get("/", params={**FORM_BLANKS, "unscored": ""})
    absent = client.get("/", params=FORM_BLANKS)
    ticked = client.get("/", params={**FORM_BLANKS, "unscored": "true"})

    assert blank.status_code == 200
    assert "Memoirs of a Dutiful Daughter" not in absent.text
    assert "Memoirs of a Dutiful Daughter" not in blank.text, "a blank box is off, not on"
    assert "Memoirs of a Dutiful Daughter" in ticked.text


@pytest.mark.parametrize(
    ("params", "why"),
    [
        ({"min_rating": "9"}, "above the 5-star ceiling"),
        ({"min_rating": "-1"}, "below zero"),
        ({"min_rating": "abc"}, "not a number at all"),
        ({"min_ratings_count": "-5"}, "a negative count"),
        ({"limit": "0"}, "a page of nothing"),
        ({"unscored": "maybe"}, "not a boolean"),
    ],
)
def test_a_genuinely_invalid_filter_is_still_rejected(
    client: TestClient, params: dict[str, str], why: str
) -> None:
    """Accepting a blank must not mean accepting anything.

    `limit=0` and `limit=-5` matter beyond tidiness: paging slices an
    already-materialised list, so a negative limit silently dropped books off
    the tail rather than erroring.
    """
    assert client.get("/", params=params).status_code == 422, why


# --- browsing -----------------------------------------------------------------


def test_paging_walks_the_whole_result_set_without_repeating(client: TestClient) -> None:
    """Filtering happens in Python over the full in-stock list, so a page is a
    slice of an already-materialised set — the risk is an off-by-one that drops
    or repeats a book at the seam, not a bad query."""
    everything = client.get("/api/books", params={"limit": "100"}).json()
    first = client.get("/api/books", params={"limit": "2", "offset": "0"}).json()
    second = client.get("/api/books", params={"limit": "2", "offset": "2"}).json()

    ids = [b["product_id"] for b in first + second]
    assert len(ids) == len(set(ids)), "a book must not appear on two pages"
    assert ids == [b["product_id"] for b in everything[:4]]


def test_the_pager_offers_next_only_when_there_is_more(client: TestClient) -> None:
    page = client.get("/", params={"unscored": "true", "limit": "2"})
    everything = client.get("/", params={"unscored": "true", "limit": "500"})

    assert "next →" in page.text
    assert "next →" not in everything.text


def test_an_offset_past_the_end_is_empty_rather_than_an_error(client: TestClient) -> None:
    response = client.get("/", params={"unscored": "true", "offset": "9999"})

    assert response.status_code == 200
    assert 'class="book"' not in response.text


def test_a_filter_link_carries_the_rest_of_the_filter_state(client: TestClient) -> None:
    """Clicking a category used to drop the search that found it, because the
    link was a bare `?tag=`. Every link is now built from the whole state."""
    response = client.get("/", params={"q": "beauvoir", "sort": "price", "unscored": "true"})
    links = re.findall(r'href="(/\?[^"]*category=[^"]*)"', response.text)

    assert links, "expected at least one category link on the page"
    for link in links:
        assert "q=beauvoir" in link and "sort=price" in link


def test_excluding_a_category_removes_those_books(client: TestClient) -> None:
    """The browse-side answer to manga dominating the ranking. Categories are
    used rather than tags because they cover the whole catalogue."""
    fiction = client.get("/api/books", params={"category": "Non Fiction"}).json()
    assert fiction, "the fixture should carry at least one Non Fiction listing"

    without = client.get("/api/books", params={"exclude_category": "Non Fiction"}).json()

    excluded = {b["product_id"] for b in fiction}
    assert excluded and not ({b["product_id"] for b in without} & excluded)


def test_every_book_reports_when_it_arrived(client: TestClient) -> None:
    """`date_created` is NULL until the sweep backfills it, so the card falls
    back to `first_seen_at` — flagged, because they mean different things."""
    books = client.get("/api/books").json()

    assert all(b["added"] for b in books)
    assert all(b["added_estimated"] for b in books), "the fixture has no wp/v2 dates yet"
