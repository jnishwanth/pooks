"""What actually goes on the wire to Telegram.

The digest was the one output nothing exercised. Every defect these cover was
found by reading rather than by a failing test: shop text reaching an HTML
payload unescaped, a message with no length budget against a hard 4,096-character
limit, and a `TelegramError` handler that drops a chunk permanently because
`process_pending` has already marked its events processed.

The bot is faked rather than mocked at the transport, so the assertions are on
the keyword arguments `send_message` would receive — `LinkPreviewOptions` and
`InlineKeyboardMarkup` stay real, since they are pure data and are what the API
would actually be handed.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from pooks.db.store import Store
from pooks.enrich.sources import BookFacts, IndianPrice
from pooks.llm.pipeline import BookInsights
from pooks.models import Product
from pooks.notify.telegram import (
    BLURB_LIMIT,
    TEXT_LIMIT,
    TelegramNotifier,
    chunk_books,
    plain_text,
    render_digest,
)
from pooks.rank.score import ScoreBreakdown
from pooks.run import ProcessedBook

COVER = "https://oldbookdepot.in/wp-content/uploads/2026/08/cover.jpg"


def _book(
    product_id: int = 1,
    *,
    name: str = "A History of Cambodia",
    permalink: str | None = "https://oldbookdepot.in/product/a-history-of-cambodia",
    image_url: str | None = COVER,
    condition: str | None = "Very Good",
    price_paise: int | None = 39_900,
    blurb: str | None = None,
    spoiler_flagged: bool = False,
    author: str | None = "David Chandler",
    tags: dict[str, list[str]] | None = None,
    indian_price: IndianPrice | None = None,
    previous_price_paise: int | None = None,
) -> ProcessedBook:
    product = Product(
        product_id=product_id,
        name=name,
        permalink=permalink,
        image_url=image_url,
        author=author,
        condition=condition,
        price_paise=price_paise,
        in_stock=True,
    )
    return ProcessedBook(
        product=product,
        facts=BookFacts(
            book_key=product.book_key,
            resolved_author=author,
            tags=tags,
            indian_price=indian_price,
        ),
        insights=BookInsights(blurb=blurb, spoiler_flagged=spoiler_flagged),
        breakdown=ScoreBreakdown(
            score=0.8, quality=0.7, renown=0.6, value=0.5, condition_factor=0.93, confidence=0.7
        ),
        event_id=product_id,
        event_type="NEW_IN_STOCK",
        notify=True,
        previous_price_paise=previous_price_paise,
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `send_message` call the notifier makes, as its keyword arguments.

    `send` builds its own `Bot`, so replacing the name in the module is enough
    and no credential or network is involved.
    """
    calls: list[dict[str, Any]] = []

    class _Bot:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs: Any) -> object:
            calls.append(kwargs)
            return object()

    monkeypatch.setattr("pooks.notify.telegram.Bot", _Bot)
    return calls


def _notifier(max_per_message: int = 10) -> TelegramNotifier:
    return TelegramNotifier("token", "chat", max_per_message)


# --- the cover ---------------------------------------------------------------


async def test_a_message_about_one_book_shows_its_cover_above_the_text(
    store: Store, sent: list[dict[str, Any]]
) -> None:
    """The shop photographs the actual copy, which is the single most useful
    thing a push can carry — but a preview belongs to a message, not a line in
    one, so it is shown exactly when the message is about one book."""
    await _notifier().send(store, [_book()])

    # Asserted through `to_dict` because that is the payload, and because an
    # unset PTB optional is `DefaultValue(None)` — it reprs as `None` and is not
    # `None`, so an `is None` assertion here passes for the wrong reason.
    assert sent[0]["link_preview_options"].to_dict() == {
        "url": COVER,
        "prefer_large_media": True,
        "show_above_text": True,
    }


async def test_a_message_about_several_books_shows_no_cover(
    store: Store, sent: list[dict[str, Any]]
) -> None:
    """The boundary that pins the rule's direction: two books, both with covers,
    and neither may be passed off as the subject of the whole message."""
    await _notifier().send(store, [_book(1), _book(2)])

    assert sent[0]["link_preview_options"].to_dict() == {"is_disabled": True}


async def test_a_book_with_no_cover_disables_the_preview(
    store: Store, sent: list[dict[str, Any]]
) -> None:
    """Not merely "no url": the titles are links now, so leaving the preview
    enabled would have Telegram preview the shop page instead — which carries no
    OpenGraph tags and renders as an empty card."""
    await _notifier().send(store, [_book(image_url=None)])

    assert sent[0]["link_preview_options"].to_dict() == {"is_disabled": True}


# --- the buttons -------------------------------------------------------------


async def test_every_book_gets_a_button_pointing_at_its_own_listing(
    store: Store, sent: list[dict[str, Any]]
) -> None:
    books = [_book(i, permalink=f"https://oldbookdepot.in/product/{i}") for i in (1, 2, 3)]
    await _notifier().send(store, books)

    rows = sent[0]["reply_markup"].inline_keyboard
    assert [button.url for row in rows for button in row] == [b.product.permalink for b in books]


async def test_a_listing_with_no_link_contributes_no_button(
    store: Store, sent: list[dict[str, Any]]
) -> None:
    """Built by filtering rather than by index. Numbering buttons off their
    position in the keyboard would disagree with the card above as soon as one
    book had nothing to point at."""
    await _notifier().send(store, [_book(1), _book(2, permalink=None), _book(3)])

    rows = sent[0]["reply_markup"].inline_keyboard
    assert [button.text.split(".")[0] for row in rows for button in row] == ["1", "3"]


async def test_a_message_where_nothing_can_be_bought_carries_no_keyboard(
    store: Store, sent: list[dict[str, Any]]
) -> None:
    await _notifier().send(store, [_book(permalink=None)])

    assert sent[0]["reply_markup"] is None


async def test_a_button_label_carries_the_shop_s_text_unescaped(
    store: Store, sent: list[dict[str, Any]]
) -> None:
    """Button text is drawn literally, not parsed as HTML. Escaping it the way
    the card is escaped would show a reader `Tom &amp; Jerry`."""
    await _notifier().send(store, [_book(name="Tom & Jerry")])

    label = sent[0]["reply_markup"].inline_keyboard[0][0].text
    assert "Tom & Jerry" in label
    assert "&amp;" not in label


# --- the card ----------------------------------------------------------------


def test_the_title_is_the_link() -> None:
    text = render_digest([_book(permalink="https://oldbookdepot.in/product/x")])

    assert '<a href="https://oldbookdepot.in/product/x">A History of Cambodia</a>' in text
    assert ">buy</a>" not in text


def test_a_book_with_no_listing_link_still_renders_its_title() -> None:
    text = render_digest([_book(permalink=None)])

    assert "A History of Cambodia" in text
    assert "<a href" not in text


def test_the_blurb_is_quoted_rather_than_run_into_the_facts() -> None:
    text = render_digest([_book(blurb="A dry, meticulous history.")])

    assert "<blockquote>A dry, meticulous history.</blockquote>" in text


def test_a_blurb_collapses_only_where_there_is_something_to_scroll_past() -> None:
    """A digest can hide a blurb behind "show more"; the solo card that also
    carries the cover must not, or it folds away its own point."""
    solo = render_digest([_book(1, blurb="One.")])
    digest = render_digest([_book(1, blurb="One."), _book(2, blurb="Two.")])

    assert "<blockquote>" in solo and "expandable" not in solo
    assert "<blockquote expandable>" in digest


def test_a_blurb_flagged_for_spoilers_is_not_shown() -> None:
    """`llm.roles` already replaces flagged text with a placeholder sentence, so
    what this suppresses is that placeholder being pushed as if it were a
    summary. The unflagged half pins the direction."""
    placeholder = "No spoiler-free summary could be produced for this title."

    flagged = render_digest([_book(blurb=placeholder, spoiler_flagged=True)])
    clean = render_digest([_book(blurb=placeholder, spoiler_flagged=False)])

    assert placeholder not in flagged
    assert "<blockquote" not in flagged
    assert placeholder in clean


def test_a_runaway_blurb_cannot_cost_a_whole_message() -> None:
    """The blurb is the card's only unbounded field, so it is the only way one
    book could exceed the message limit and take its siblings with it."""
    text = render_digest([_book(blurb="x" * (BLURB_LIMIT * 4))])

    assert len(text) < TEXT_LIMIT
    assert "…" in text


def test_every_shop_value_reaches_the_card_escaped() -> None:
    """Owns the rule rather than the sites, so an interpolation added later is
    covered without editing this test.

    `models.clean_text` html-unescapes shop text on the way in, so a bare `&`
    genuinely reaches here. Unescaped it is a `can't parse entities` rejection,
    and the handler logs it and moves on — the books in that chunk are never
    pushed, because their events are already marked processed.
    """
    book = _book(
        name="Tom & Jerry <Deluxe>",
        author="A. & B. Smith",
        condition="Good & clean",
        tags={"genre": ["crime & mystery"]},
        blurb="Wit & grit <throughout>",
        indian_price=IndianPrice(
            price_paise=99_900, source="books&more.in", available_in_india=True
        ),
    )
    text = render_digest([book])

    # Every value survives a round trip through the terminal renderer...
    readable = plain_text(text)
    for value in ("Tom & Jerry <Deluxe>", "A. & B. Smith", "Good & clean", "crime & mystery"):
        assert value in readable
    assert "Wit & grit <throughout>" in readable

    # ...and once the tags are removed, nothing is left that Telegram's entity
    # parser could choke on: no bare angle bracket, and every `&` opens a real
    # entity.
    content = re.sub(r"<[^>]+>", "", text)
    assert "<" not in content and ">" not in content
    assert re.search(r"&(?!amp;|lt;|gt;|quot;|#x27;|#\d+;)", content) is None


# --- chunking ----------------------------------------------------------------


def test_a_drop_is_split_by_the_book_cap_with_ranks_that_keep_counting() -> None:
    books = [_book(i) for i in range(1, 13)]

    chunks = list(chunk_books(books, 5))

    assert [len(chunk) for _, chunk in chunks] == [5, 5, 2]
    assert [offset for offset, _ in chunks] == [0, 5, 10]


def test_a_digest_is_split_before_telegram_would_reject_it() -> None:
    """The book cap alone was the whole budget, against a hard 4,096-character
    limit that answers an over-long message with a 400. A blockquoted blurb
    makes ten cards comfortably exceed it."""
    books = [_book(i, blurb="x" * BLURB_LIMIT) for i in range(1, 13)]

    chunks = list(chunk_books(books, 50))

    assert len(chunks) > 1, "a book cap of 50 means only the length budget can split these"
    assert all(
        len(render_digest(chunk, offset=offset, total=len(books))) <= TEXT_LIMIT
        for offset, chunk in chunks
    )
    assert sum(len(chunk) for _, chunk in chunks) == len(books)


def test_a_digest_that_exactly_fills_a_message_is_not_split() -> None:
    """The boundary, built rather than guessed: a title is padded one character
    at a time until the rendered digest is exactly `TEXT_LIMIT`.

    Telegram accepts a message *of* the limit and rejects one over it, so an
    off-by-one either splits a message that would have fitted — cosmetic — or
    sends one that is rejected whole, which loses its books permanently.
    """
    head = [_book(i, blurb="x" * BLURB_LIMIT) for i in range(1, 4)]

    def _filled(pad: int) -> list[ProcessedBook]:
        return [*head, _book(4, name="A History of Cambodia" + "x" * pad, blurb="x" * BLURB_LIMIT)]

    pad = 0
    while len(render_digest(_filled(pad), total=4)) < TEXT_LIMIT:
        pad += 1
    exactly_full = _filled(pad)
    assert len(render_digest(exactly_full, total=4)) == TEXT_LIMIT, "could not hit the boundary"

    assert [len(chunk) for _, chunk in chunk_books(exactly_full, 50)] == [4]

    # One character more is one message too many.
    over = _filled(pad + 1)
    assert len(render_digest(over, total=4)) == TEXT_LIMIT + 1
    assert [len(chunk) for _, chunk in chunk_books(over, 50)] == [3, 1]


def test_a_split_message_does_not_announce_itself_as_a_second_drop() -> None:
    """Counting per message rendered a drop of twelve as "10 new" and then "2
    new", which reads as two arrivals — the one thing grouping exists to
    avoid."""
    books = [_book(i) for i in range(1, 13)]
    chunks = list(chunk_books(books, 10))

    texts = [render_digest(chunk, offset=offset, total=len(books)) for offset, chunk in chunks]

    assert texts[0].startswith("<b>12 new at Old Book Depot</b>")
    assert "new at Old Book Depot" not in texts[1]


async def test_a_split_drop_numbers_its_books_once_each_end_to_end(
    store: Store, sent: list[dict[str, Any]]
) -> None:
    books = [_book(i) for i in range(1, 13)]

    await _notifier(5).send(store, books)

    labels = [
        button.text.split(".")[0]
        for call in sent
        for row in call["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels == [str(n) for n in range(1, 13)]


# --- sending -----------------------------------------------------------------


async def test_a_chunk_telegram_rejects_is_not_recorded_as_notified(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording before the send would mark books notified that nobody ever
    saw, and nothing retries them: `process_pending` marks the events processed
    regardless of what the push did."""
    from telegram.error import TelegramError

    calls: list[dict[str, Any]] = []

    class _Bot:
        def __init__(self, token: str) -> None: ...

        async def send_message(self, **kwargs: Any) -> object:
            calls.append(kwargs)
            if len(calls) == 1:
                raise TelegramError("can't parse entities")
            return object()

    monkeypatch.setattr("pooks.notify.telegram.Bot", _Bot)

    books = [_book(i) for i in range(1, 5)]
    sent_count = await _notifier(2).send(store, books)

    assert sent_count == 2
    assert not store.already_notified(1, 1)
    assert store.already_notified(3, 3)


async def test_nothing_is_sent_or_recorded_without_credentials(
    store: Store, sent: list[dict[str, Any]]
) -> None:
    count = await TelegramNotifier(None, None).send(store, [_book()])

    assert count == 0
    assert sent == []
    assert not store.already_notified(1, 1)


async def test_an_empty_drop_sends_nothing(store: Store, sent: list[dict[str, Any]]) -> None:
    assert await _notifier().send(store, []) == 0
    assert sent == []


# --- the terminal ------------------------------------------------------------


def test_the_terminal_sees_the_message_without_its_markup() -> None:
    """`pooks notify --dry-run` and `pooks health` both print what would be
    sent. `health` stripped `<b>` by hand, which a blockquote or a `<pre>` block
    silently defeats."""
    text = render_digest([_book(blurb="A dry, meticulous history.")])

    readable = plain_text(text)

    assert "<" not in readable
    assert "A dry, meticulous history." in readable
    assert "A History of Cambodia" in readable
