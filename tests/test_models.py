from __future__ import annotations

from enum import StrEnum

from pooks.models import (
    EventType,
    Product,
    clean_text,
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
