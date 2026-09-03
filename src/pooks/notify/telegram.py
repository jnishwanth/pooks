"""Telegram push.

Books at this shop sell fast, so a find is worth pushing immediately rather
than batching into a daily digest. But arrivals come in bulk uploads (recon saw
0 one day and 8 over three), so messages are grouped: one drop must not become
thirty notifications.

The card is written for Telegram specifically rather than as plain text that
happens to be sent there. The blurb is a `<blockquote>`, the title carries the
link, each book gets a tappable button, and a message holding a single book
shows the shop's photograph of that copy above the text. Grouping is why the
photograph is conditional: a link preview belongs to a message, not to a line
in one, so a digest of ten books has no honest way to show ten covers.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import MessageLimit, ParseMode
from telegram.error import TelegramError

from pooks.config import Config
from pooks.db.store import Store, transaction
from pooks.run import ProcessedBook

log = logging.getLogger(__name__)

# Telegram measures this in UTF-16 code units *after* entity parsing, so `len`
# over the markup is conservative twice over: the tags are counted here and do
# not survive parsing, and every character the card uses is BMP (one unit
# each). Keep it that way — an astral emoji is one Python character and two
# UTF-16 units, the one direction in which `len` could under-count.
TEXT_LIMIT = int(MessageLimit.MAX_TEXT_LENGTH)

# A card can only overrun the message limit through the blurb; every other
# field is bounded by the shop or by enrichment. Roughly three times the 2-3
# sentences the prompt asks for, so it never fires on a well-behaved blurb and
# still keeps one book from costing a whole message.
BLURB_LIMIT = 800

# Long enough to recognise the book, short enough not to wrap on a phone.
BUTTON_TITLE_LIMIT = 28

_TAG = re.compile(r"<[^>]+>")


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None, max_per_message: int = 10) -> None:
        self.token = token
        self.chat_id = chat_id
        self.max_per_message = max_per_message

    @classmethod
    def from_config(cls, config: Config) -> TelegramNotifier:
        """The one way to build a notifier from configuration.

        Three call sites repeated the two secrets, and `pooks health` omitted
        the chunk size — latent only because a health digest is a single
        message that `send_text` never chunks.
        """
        return cls(
            config.secrets.telegram_bot_token,
            config.secrets.telegram_chat_id,
            config.max_books_per_message,
        )

    def _credentials(self) -> tuple[str, str] | None:
        """Token and chat id together, or None if either is missing.

        One definition of "configured", so the property and the two send paths
        cannot disagree — and unlike a boolean it narrows both optional fields
        for the caller, which is what the API actually needs.
        """
        if self.token and self.chat_id:
            return self.token, self.chat_id
        return None

    @property
    def configured(self) -> bool:
        return self._credentials() is not None

    async def send(self, store: Store, books: list[ProcessedBook]) -> int:
        if not books:
            return 0

        credentials = self._credentials()
        if credentials is None:
            log.info(
                "telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID); "
                "%d book(s) would have been pushed",
                len(books),
            )
            return 0
        token, chat_id = credentials

        sent = 0
        bot = Bot(token=token)

        for offset, chunk in chunk_books(books, self.max_per_message):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=render_digest(chunk, offset=offset, total=len(books)),
                    parse_mode=ParseMode.HTML,
                    link_preview_options=cover_preview(chunk),
                    reply_markup=buy_buttons(chunk, offset=offset),
                )
            except TelegramError as exc:
                log.error("telegram send failed: %s", exc)
                continue

            with transaction(store.conn):
                for book in chunk:
                    store.record_notification(book.product.product_id, book.event_id)
            sent += len(chunk)

        return sent

    async def send_text(self, text: str) -> bool:
        """Send an arbitrary message — used by the health digest."""
        credentials = self._credentials()
        if credentials is None:
            log.info("telegram not configured; health digest not sent")
            return False
        token, chat_id = credentials
        try:
            await Bot(token=token).send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramError as exc:
            log.error("telegram health digest failed: %s", exc)
            return False
        return True


def chunk_books(
    books: list[ProcessedBook], max_per_message: int
) -> Iterator[tuple[int, list[ProcessedBook]]]:
    """Group books into messages, yielding each chunk with its rank offset.

    Two caps, not one. The book count keeps a bulk upload readable; the
    character count is Telegram's own, and exceeding it fails the whole
    message. That failure is silent and permanent — `process_pending` has
    already marked the events handled, so a rejected chunk is never retried and
    those books are simply never pushed — which is why the budget is enforced
    here rather than left to chance.

    A candidate is measured as if it were staying multi-book, which is the
    longer rendering (`<blockquote expandable>`). A chunk that ends up alone
    therefore renders shorter than it was measured at, so the estimate can only
    err towards sending.
    """
    chunk: list[ProcessedBook] = []
    offset = 0

    for book in books:
        candidate = [*chunk, book]
        overruns = len(candidate) > max_per_message or (
            len(render_digest(candidate, offset=offset, total=len(books))) > TEXT_LIMIT
        )
        # `chunk` being empty means this book is alone and still over budget.
        # Yielding here would emit an empty message and lose it, so it is kept:
        # the blurb cap is what keeps a lone card inside the limit anyway.
        if chunk and overruns:
            yield offset, chunk
            offset += len(chunk)
            chunk = [book]
        else:
            chunk = candidate

    if chunk:
        yield offset, chunk


def cover_preview(books: list[ProcessedBook]) -> LinkPreviewOptions:
    """The shop's photograph of the copy, shown above a single-book message.

    The rule is "this message carries one book", not "the best book in it": a
    preview belongs to the whole message, so anything else would attach one
    book's photograph to a card listing nine others. It also means a chunk that
    ended up alone because of the length budget still gets its cover, rather
    than the outcome depending on why it was alone.

    `prefer_large_media` and `show_above_text` are ignored by Telegram unless
    `url` is set explicitly, so they travel with it or not at all.
    """
    if len(books) != 1 or not books[0].product.image_url:
        return LinkPreviewOptions(is_disabled=True)
    return LinkPreviewOptions(
        url=books[0].product.image_url,
        prefer_large_media=True,
        show_above_text=True,
    )


def buy_buttons(books: list[ProcessedBook], offset: int = 0) -> InlineKeyboardMarkup | None:
    """A tappable row per book, or None when none of them can be bought.

    Built by filtering rather than by index: a listing without a permalink
    contributes no button, and numbering the buttons off their position in the
    keyboard would then disagree with the card above it.
    """
    rows = [
        [InlineKeyboardButton(text=_button_label(book, offset + i + 1), url=permalink)]
        for i, book in enumerate(books)
        if (permalink := book.product.permalink)
    ]
    return InlineKeyboardMarkup(rows) if rows else None


def _button_label(book: ProcessedBook, rank: int) -> str:
    """Button text is drawn as-is, so it is truncated rather than escaped."""
    title = book.product.work_title
    if len(title) > BUTTON_TITLE_LIMIT:
        title = title[: BUTTON_TITLE_LIMIT - 1].rstrip() + "…"
    price = book.product.price_inr
    return f"{rank}. {title}" + (f" · ₹{price:.0f}" if price else "")


def plain_text(markup: str) -> str:
    """The same message as the terminal should show it.

    `pooks notify --dry-run` and `pooks health` both print what would be sent,
    and both were stripping tags by hand — `health` with a two-tag `.replace`
    that a blockquote or a `<pre>` block silently defeats. One definition, so
    adding a tag to the card cannot leave a command printing raw markup.
    """
    return html.unescape(_TAG.sub("", markup))


def render_digest(books: list[ProcessedBook], offset: int = 0, total: int | None = None) -> str:
    """One message: the cards for `books`, numbered from `offset`.

    The header names the whole drop and is written once, on the message that
    opens it. Counting per message instead announced a drop of twelve as "10
    new" and then "2 new", which reads as two separate arrivals — the one thing
    the grouping exists to avoid.
    """
    # A blurb collapses only where there is something to scroll past. On a solo
    # card — the one that also carries the cover — hiding it behind "show more"
    # would fold away the whole point of the message.
    expandable = len(books) > 1
    cards = [render_book(b, offset + i + 1, expandable=expandable) for i, b in enumerate(books)]
    if offset:
        return "\n\n".join(cards)
    return "\n\n".join([f"<b>{total or len(books)} new at Old Book Depot</b>", *cards])


def render_book(book: ProcessedBook, rank: int, *, expandable: bool = False) -> str:
    product = book.product
    facts = book.facts
    name = html.escape(product.work_title)
    price = f"₹{product.price_inr:.0f}" if product.price_inr else "price unknown"

    # The title is the link: one affordance instead of a bold title and a
    # trailing "buy" that pointed at the same page.
    title = f'<a href="{html.escape(product.permalink)}">{name}</a>' if product.permalink else name
    line = f"<b>{rank}. {title}</b>"

    # The shop omits the author on ~half its listings. Where the title did not
    # carry it either, enrichment usually learned it from the rating source.
    if author := (product.author or facts.resolved_author):
        line += f"\n<i>{html.escape(author)}</i>"

    bits = [price]
    if product.condition:
        # Shop-supplied text that `clean_text` has already html-unescaped, so
        # it reaches here able to carry a bare `&`. Unescaped, that is a
        # `can't parse entities` rejection and a silently dropped chunk.
        bits.append(html.escape(product.condition))
    if facts.has_rating:
        bits.append(f"{facts.rating}★ ({facts.ratings_count:,})")
    line += "\n" + " · ".join(bits)

    if tags := facts.flat_tags[:5]:
        line += "\n<i>" + " · ".join(html.escape(t.replace("-", " ")) for t in tags) + "</i>"

    if seen_before := _previously_seen(book):
        line += f"\n{seen_before}"

    if verdict := _value_verdict(book):
        line += f"\n{verdict}"

    if blurb := _blurb(book):
        quote = "<blockquote expandable>" if expandable else "<blockquote>"
        line += f"\n\n{quote}{blurb}</blockquote>"

    line += f"\n\nscore {book.breakdown.score:.2f}"
    if book.breakdown.confidence < 0.5:
        line += " (thin evidence)"
    return line


def _blurb(book: ProcessedBook) -> str | None:
    """The blurb as the card should carry it, or None.

    A flagged book has no blurb to show. `llm.roles` already discards the
    flagged text when every attempt fails, substituting "No spoiler-free summary
    could be produced for this title." — so what `spoiler_flagged` suppresses
    here is that placeholder being pushed as though it were a summary, not a
    spoiler leak. The length cap is the only thing standing between one
    pathological blurb and a message Telegram rejects whole.
    """
    blurb = book.insights.blurb
    if not blurb or book.insights.spoiler_flagged:
        return None
    if len(blurb) > BLURB_LIMIT:
        blurb = blurb[: BLURB_LIMIT - 1].rstrip() + "…"
    return html.escape(blurb)


def _previously_seen(book: ProcessedBook) -> str | None:
    """Report a book returning cheaper than the last time the shop listed it.

    Deliberately a line on the card rather than its own alert. Relists get a new
    product id, so a same-product price change almost never fires and this is
    where a drop actually surfaces — but it only becomes useful once enough
    sold-out history has accumulated, so it should not add a notification type
    of its own in the meantime.
    """
    previous = book.previous_price_paise
    price = book.product.price_paise
    if not previous or not price or previous <= price:
        return None
    return (
        f"↓ ₹{(previous - price) / 100:.0f} cheaper than when last listed (₹{previous / 100:.0f})"
    )


def _value_verdict(book: ProcessedBook) -> str | None:
    """One line on whether buying it here is worth it.

    Compared against the cheapest Indian price, since that is what the buyer
    would otherwise actually pay. An earlier version compared against AbeBooks
    in USD and reported every book as ~89% cheaper, which told the reader
    nothing.
    """
    facts = book.facts
    indian = facts.indian_price
    price = book.product.price_paise

    # Bound before the branch rather than inside it: `has_price` is a property,
    # so it cannot narrow `price_inr` away from None for the division below —
    # and it is the `> 0` half of `has_price`, not merely "is set", that keeps
    # that division safe.
    baseline = indian.price_inr if indian and indian.has_price else None

    if indian is not None and baseline is not None and price:
        shop = price / 100
        # A source name reaches the payload from enrichment, so it is escaped
        # for the same reason `condition` is.
        source = html.escape((indian.source or "india").replace("searxng:", ""))
        if baseline > shop:
            return f"↓ {100 * (1 - shop / baseline):.0f}% under {source} (₹{baseline:.0f})"
        return f"₹{baseline:.0f} on {source} — cheaper elsewhere"

    if indian and indian.unknown:
        return None

    if indian and not indian.available_in_india:
        listings = facts.scarcity.listing_count if facts.scarcity else 0
        suffix = f" · {listings} used listings worldwide" if listings else ""
        return f"not sold in India — import only{suffix}"

    if facts.in_print is False:
        listings = facts.scarcity.listing_count if facts.scarcity else 0
        return f"out of print{f' · {listings} listings worldwide' if listings else ''}"
    return None
