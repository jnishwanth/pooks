"""The dashboard's HTTP surface.

Exercised through the app rather than through `_apply_filters` because the
defect these tests exist for lived entirely in the query-parameter layer, which
a direct call to the filter function cannot reach: every search submitted from
the form was answered with 422, and the filtering code it never reached was
perfectly correct.
"""

from __future__ import annotations

import json
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


def test_the_shop_description_is_shown_where_there_is_one(client: TestClient) -> None:
    """It is the shop's own copy, so it says what the book is about; the blurb
    says what reading it is like. Both, and neither in place of the other."""
    page = client.get("/", params={"unscored": "true", "limit": "500"}).text

    assert "the grandeur of the Angkor Empire" in page
    # Five of the six fixture listings carry one, and the sixth carries none —
    # which is the shape four of 574 live in-stock listings are in.
    assert page.count('class="shopdesc"') == 5


def test_every_book_reports_when_it_arrived(client: TestClient) -> None:
    """`date_created` is NULL until the sweep backfills it, so the card falls
    back to `first_seen_at` — flagged, because they mean different things."""
    books = client.get("/api/books").json()

    assert all(b["added"] for b in books)
    assert all(b["added_estimated"] for b in books), "the fixture has no wp/v2 dates yet"


def test_author_is_rendered_on_card(client: TestClient) -> None:
    page = client.get("/", params={"unscored": "true", "q": "beauvoir"}).text
    assert '<div class="author">by Simone de Beauvoir</div>' in page


def test_mobile_drawer_and_active_filters_are_rendered(client: TestClient) -> None:
    page = client.get(
        "/",
        params={
            "unscored": "true",
            "q": "beauvoir",
            "min_rating": "4.0",
        },
    ).text
    assert 'id="filter-sheet"' in page
    assert 'id="btn-open-sheet"' in page
    assert "q: beauvoir" in page
    assert "★ ≥ 4.0" in page


def test_active_count_reflects_all_filter_criteria() -> None:
    empty = serve_app.Filters()
    assert empty.active_count == 0
    assert not empty.any_active

    active = serve_app.Filters(
        q="test",
        tags=("fiction",),
        exclude_tags=("manga",),
        categories=("History",),
        exclude_categories=("Comics",),
        min_rating=4.0,
        min_ratings_count=500,
        min_confidence=0.5,
        added_within_days=7,
        unscored=True,
    )
    assert active.active_count == 10
    assert active.any_active


def test_book_cards_lazy_load_covers_and_display_deals_and_score_bars(
    tmp_path, products: list[Product], monkeypatch
) -> None:
    """Catalogue cards must render lazy-loaded covers from store image_url,
    price savings badges when comparison prices exist, and score component bars."""
    from selectolax.parser import HTMLParser

    db_path = tmp_path / "enriched.db"
    store = Store(connect(db_path))
    for p in products:
        store.upsert_product(p)
    p0 = products[0]
    store.put_enrichment(
        p0.book_key,
        {
            "rating": 4.5,
            "ratings_count": 1200,
            "rating_source": "Goodreads",
            "provenance_json": "{}",
            "refresh_attempts": 0,
            "in_price_paise": 150000,
            "in_price_source": "Amazon India",
            "in_available": 1,
            "in_price_unknown": 0,
            "tags_json": json.dumps({"genre": ["history"]}),
        },
    )
    store.put_score(
        p0.product_id,
        {
            "score": 0.85,
            "quality": 0.88,
            "renown": 0.90,
            "value": 0.78,
            "condition_factor": 1.0,
            "confidence": 0.92,
            "notes": {},
        },
    )
    store.conn.commit()
    store.conn.close()

    config = load_config()
    monkeypatch.setattr(serve_app, "_open", lambda: (config, Store(connect(db_path))))
    client = TestClient(serve_app.app)
    response = client.get("/")
    assert response.status_code == 200

    dom = HTMLParser(response.text)
    cards = dom.css("article.book")
    assert cards, "expected book cards on the page"

    card = cards[0]
    cover_img = card.css_first(".cover-box .cover-img")
    assert cover_img is not None
    assert cover_img.attributes.get("loading") == "lazy"
    assert cover_img.attributes.get("src") == p0.image_url

    author = card.css_first(".author")
    assert author is not None
    assert author.text().strip().startswith("by ")

    deal = card.css_first(".deal-pill")
    assert deal is not None
    assert "% under ₹1500 on Amazon India" in deal.text()

    bars = card.css(".bars .bar")
    assert len(bars) == 4
    for b in bars:
        assert b.css_first(".track .fill") is not None


def test_applied_filters_overview_dismissible_pills_for_all_active_constraints(
    client: TestClient,
) -> None:
    """Every active constraint generates a dismissible pill that removes only that constraint."""
    from selectolax.parser import HTMLParser

    params = {
        "q": "history",
        "tag": "non-fiction",
        "exclude_tag": "manga",
        "category": "Non Fiction",
        "exclude_category": "Comics",
        "min_rating": "4.0",
        "min_ratings_count": "500",
        "min_confidence": "0.6",
        "added_within_days": "14",
        "unscored": "true",
    }
    response = client.get("/", params=params)
    assert response.status_code == 200
    dom = HTMLParser(response.text)

    # Filter trigger badge count matches 10 active criteria
    badge = dom.css_first(".btn-filter-trigger .badge")
    assert badge is not None
    assert badge.text().strip() == "10"

    pills = dom.css(".active-filters-wrap .filter-pill")
    assert len(pills) == 10

    # Dismissing q removes q
    q_pill = [p for p in pills if "q: history" in p.text()][0]
    assert "q=" not in q_pill.attributes.get("href", "")
    assert "min_rating=4.0" in q_pill.attributes.get("href", "")

    # Dismissing rating clears min_rating
    rating_pill = [p for p in pills if "★ ≥ 4.0" in p.text()][0]
    assert "min_rating=" not in rating_pill.attributes.get("href", "")

    # Dismissing unscored clears unscored
    unscored_pill = [p for p in pills if "incl. unscored" in p.text()][0]
    assert "unscored=" not in unscored_pill.attributes.get("href", "")

    # Clear all links back to clean catalogue root
    clear_all = dom.css_first(".clear-all-link")
    assert clear_all is not None
    assert clear_all.attributes.get("href") == "/"


def test_bottom_sheet_drawer_instant_links_and_sticky_header(client: TestClient) -> None:
    """Mobile bottom sheet carries instant filter links and sticky search panel."""
    from selectolax.parser import HTMLParser

    response = client.get("/", params={"unscored": "true", "q": "cambodia"})
    assert response.status_code == 200
    dom = HTMLParser(response.text)

    # Search bar carries search placeholder
    search_input = dom.css_first("#search-input")
    assert search_input is not None
    assert "Search title or author" in search_input.attributes.get("placeholder", "")

    # Mobile sheet contains modal drawer, backdrop, and instant navigation links
    sheet = dom.css_first("#filter-sheet")
    assert sheet is not None
    assert dom.css_first("#sheet-backdrop") is not None
    sheet_links = sheet.css(".sheet-section a")
    assert len(sheet_links) > 0
    for link in sheet_links:
        href = link.attributes.get("href", "")
        assert "q=cambodia" in href
        assert "unscored=true" in href


def test_zero_external_dependencies(client: TestClient) -> None:
    """Pure semantic HTML5/CSS3 and minimal inline vanilla JS without external CDN
    scripts or styles."""
    from selectolax.parser import HTMLParser

    response = client.get("/", params={"unscored": "true"})
    assert response.status_code == 200
    dom = HTMLParser(response.text)

    for script in dom.css("script"):
        src = script.attributes.get("src")
        assert not src or not (
            src.startswith("http://") or src.startswith("https://") or src.startswith("//")
        ), f"Found external script dependency: {src}"

    for link in dom.css('link[rel="stylesheet"]'):
        href = link.attributes.get("href")
        assert not href or not (
            href.startswith("http://") or href.startswith("https://") or href.startswith("//")
        ), f"Found external stylesheet dependency: {href}"


def test_dashboard_renders_clean_tag_chips_and_filters_unmigrated_uuid_tags(
    tmp_path, products: list[Product], monkeypatch
) -> None:
    """End-to-end verification of user intent: books with UUID-suffixed tags
    (e.g. user-submitted tags from Hardcover) render clean chips without UUIDs,
    deduplicate repeated tags, and match clean tag filter queries."""
    from selectolax.parser import HTMLParser

    db_path = tmp_path / "legacy_tags.db"
    store = Store(connect(db_path))
    p0 = products[0]
    store.upsert_product(p0)
    dirty_tags = {
        "genre": ["mafia-1e59fab6-82ef-49cd-9ebb-e877f8bad176", "crime"],
        "mood": ["tense"],
        "tags": [
            "classics-a6d38e19-11a4-42a5-8bd4-76960d21479d",
            "classics",
        ],
    }
    store.put_enrichment(
        p0.book_key,
        {
            "provenance_json": "{}",
            "refresh_attempts": 0,
            "tags_json": json.dumps(dirty_tags),
        },
    )
    store.put_score(
        p0.product_id,
        {
            "score": 0.85,
            "quality": 0.88,
            "renown": 0.90,
            "value": 0.78,
            "condition_factor": 1.0,
            "confidence": 0.92,
            "notes": {},
        },
    )
    store.conn.commit()
    store.conn.close()

    config = load_config()
    monkeypatch.setattr(serve_app, "_open", lambda: (config, Store(connect(db_path))))
    client = TestClient(serve_app.app)

    # 1. Verify unfiltered catalogue page renders clean, deduplicated chips
    response = client.get("/")
    assert response.status_code == 200
    dom = HTMLParser(response.text)
    chips = [c.text().strip() for c in dom.css("article.book a.chip")]
    assert "mafia" in chips
    assert "crime" in chips
    assert "tense" in chips
    assert "classics" in chips
    # Assert no UUID hashes leaked into chips
    assert not any("1e59fab6" in c or "a6d38e19" in c for c in chips)
    # Assert classics is deduplicated (appears only once on the book)
    assert chips.count("classics") == 1

    # 2. Verify filter by 'classics' matches the book that had 'classics-<uuid>'
    filter_res = client.get("/", params={"tag": "classics"})
    assert filter_res.status_code == 200
    filter_dom = HTMLParser(filter_res.text)
    books = filter_dom.css("article.book")
    assert len(books) == 1
    assert p0.name in books[0].text()

    # 3. Verify filter by 'mafia' matches the book that had 'mafia-<uuid>'
    mafia_res = client.get("/", params={"tag": "mafia"})
    assert mafia_res.status_code == 200
    mafia_dom = HTMLParser(mafia_res.text)
    mafia_books = mafia_dom.css("article.book")
    assert len(mafia_books) == 1
    assert p0.name in mafia_books[0].text()
