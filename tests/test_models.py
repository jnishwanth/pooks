from __future__ import annotations

from enum import StrEnum

from pooks.models import (
    EventType,
    Product,
    clean_text,
    first_image,
    html_to_text,
    make_book_key,
    normalise_isbn,
    notifiable,
    strip_title,
)


def test_parses_real_payload(products: list[Product]) -> None:
    by_id = {p.product_id: p for p in products}
    cambodia = by_id[233188]

    assert cambodia.name == "A History of Cambodia by David Chandler"
    assert cambodia.author == "David Chandler"
    assert cambodia.in_stock is True
    assert cambodia.price_paise == 39900
    assert cambodia.price_inr == 399.0
    assert cambodia.condition in {"Very Good", "Good", "New", "Fair"}


def test_html_entities_are_unescaped(products: list[Product]) -> None:
    """Categories arrive entity-encoded ('Literature &amp; Fiction') and would
    corrupt downstream search queries if passed through raw."""
    all_categories = {c for p in products for c in p.categories}
    assert all_categories, "fixture should carry categories"
    assert not any("&amp;" in c for c in all_categories)


def test_the_shop_description_is_read_as_text(products: list[Product]) -> None:
    """The shop writes real markup, and on most listings it is markup pasted out
    of a chat UI — nested divs, spans and `data-url` attributes wrapping the
    prose. What reaches a blurb prompt or a search index has to be the words."""
    by_id = {p.product_id: p for p in products}
    cambodia = by_id[233188].description

    assert cambodia is not None
    assert cambodia.startswith("A History of Cambodia by David Chandler is a definitive account")
    assert "the grandeur of the Angkor Empire" in cambodia
    # None of the carrier survives: no tags, no class names, no link targets.
    assert "<" not in cambodia
    assert "ai-message-item" not in cambodia
    assert "ca://" not in cambodia


def test_a_listing_with_no_description_has_none(products: list[Product]) -> None:
    """Four of 574 in-stock listings carry no description at all, so the absent
    case is real rather than hypothetical."""
    by_id = {p.product_id: p for p in products}
    assert by_id[233107].description is None


def test_a_description_with_no_words_in_it_is_none() -> None:
    """`None` and `""` would be two spellings of the same fact, and every reader
    downstream would then have to test for both. Empty markup counts as empty:
    `<p></p>` is what a cleared description leaves behind."""
    for raw in (None, "", "   ", "<p></p>", "<div><span></span></div>"):
        assert html_to_text(raw) is None

    payload = {"id": 1, "name": "A Book", "prices": {}, "is_in_stock": True}
    assert Product.from_store_api(payload).description is None
    assert Product.from_store_api({**payload, "description": ""}).description is None
    assert Product.from_store_api({**payload, "description": "<p></p>"}).description is None


def test_a_description_is_unescaped_and_collapsed_like_every_other_field() -> None:
    """Same treatment as categories, which arrive as 'Literature &amp; Fiction'."""
    assert html_to_text("<p>Tea &amp; Sympathy</p>\n<p>Volume\n  two</p>") == (
        "Tea & Sympathy Volume two"
    )


def test_strip_title_removes_author_and_edition_suffixes() -> None:
    assert (
        strip_title(
            "This Way for the Gas, Ladies and Gentlemen by Tadeusz Borowski (Penguin Classics)"
        )
        == "This Way for the Gas, Ladies and Gentlemen"
    )
    assert strip_title("The First Anglo-Sikh War by Amarpal Singh (Hardcover)") == (
        "The First Anglo-Sikh War"
    )
    assert strip_title("Naruto 30 by Masashi Kishimoto") == "Naruto 30"


def test_strip_title_keeps_titles_without_author_suffix() -> None:
    assert strip_title("Dirty Tricks") == "Dirty Tricks"


def test_normalise_isbn() -> None:
    assert normalise_isbn("978-0-14-018624-6") == "9780140186246"
    assert normalise_isbn("9780140186246") == "9780140186246"
    assert normalise_isbn("0140186247") == "0140186247"
    assert normalise_isbn("12345") is None
    assert normalise_isbn(None) is None


def test_book_key_prefers_isbn() -> None:
    assert make_book_key("978-0-14-018624-6", "Anything", "Anyone") == "isbn:9780140186246"


def test_book_key_falls_back_to_title_author() -> None:
    """~7% of in-stock items carry no ISBN; they must still cache."""
    key = make_book_key(None, "Sexual Politics by Kate Millett", "Kate Millett")
    assert key == "ta:sexual-politics|kate-millett"


def test_book_key_is_stable_across_edition_suffixes() -> None:
    a = make_book_key(None, "Plays by Alexander Ostrovsky (Vintage 1974 Hardcover)", "A. Ostrovsky")
    b = make_book_key(None, "Plays by Alexander Ostrovsky", "A. Ostrovsky")
    assert a == b


def test_clean_text_collapses_whitespace() -> None:
    assert clean_text("  Literature &amp;   Fiction \n") == "Literature & Fiction"
    assert clean_text(None) is None
    assert clean_text("") is None


def test_notifiable_accepts_a_member_and_the_string_a_row_stores() -> None:
    """`run.process_pending` passes the `event_type` column, `diff` passes the
    member; both callers decide the same push."""
    assert notifiable(EventType.NEW_IN_STOCK, backfill=False)
    assert notifiable(str(EventType.BACK_IN_STOCK), backfill=False)
    assert not notifiable(str(EventType.SOLD_OUT), backfill=False)
    assert not notifiable(str(EventType.NEW_IN_STOCK), backfill=True)


def test_notifiable_matches_on_the_members_value_not_its_name(monkeypatch) -> None:
    """A stored row holds the value, and today every member's name equals it, so
    nothing here would notice the lookup drifting to names — which is what a
    plain `Enum` would do, silently pushing nothing at all."""
    from pooks import models

    class Renamed(StrEnum):
        NEW_IN_STOCK = "new_in_stock"

    monkeypatch.setattr(models, "NOTIFY_EVENTS", frozenset({Renamed.NEW_IN_STOCK}))

    assert models.notifiable("new_in_stock", backfill=False)
    assert not models.notifiable("NEW_IN_STOCK", backfill=False)


def test_the_cover_is_taken_from_the_shop_s_own_photograph(products: list[Product]) -> None:
    """Every in-stock listing carries one, because the shop photographs the
    actual copy rather than reusing a stock jacket."""
    cambodia = next(p for p in products if p.product_id == 233188)

    assert cambodia.image_url == (
        "https://oldbookdepot.in/wp-content/uploads/2026/08/"
        "A-History-of-Cambodia-by-David-Chandler.jpg"
    )


def test_a_listing_with_no_usable_photograph_has_no_cover() -> None:
    """All four shapes yield None rather than a value Telegram would reject.

    The relative path is the one with teeth: it is a plausible payload, and a
    preview URL Telegram cannot resolve is answered with a 400 that drops the
    whole message — every book in it, permanently.
    """
    assert first_image({"id": 1}) is None
    assert first_image({"id": 1, "images": []}) is None
    assert first_image({"id": 1, "images": [{"id": 9}]}) is None
    assert first_image({"id": 1, "images": [{"src": "/wp-content/uploads/x.jpg"}]}) is None


def test_a_photograph_url_is_taken_as_the_shop_gives_it() -> None:
    """Not through `clean_text`: it NFKC-normalises and unescapes entities,
    which is right for prose and wrong for a URL."""
    assert first_image({"images": [{"src": "  https://x/y.jpg  "}]}) == "https://x/y.jpg"
