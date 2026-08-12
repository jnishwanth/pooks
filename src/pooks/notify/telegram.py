"""Telegram push.

Books at this shop sell fast, so a find is worth pushing immediately rather
than batching into a daily digest. But arrivals come in bulk uploads (recon saw
0 one day and 8 over three), so messages are grouped: one drop must not become
thirty notifications.
"""

from __future__ import annotations

import html
import logging

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from pooks.db.store import Store, transaction
from pooks.run import ProcessedBook

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None, max_per_message: int = 10) -> None:
        self.token = token
        self.chat_id = chat_id
        self.max_per_message = max_per_message

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, store: Store, books: list[ProcessedBook]) -> int:
        if not books:
            return 0

        if not self.configured:
            log.info(
                "telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID); "
                "%d book(s) would have been pushed",
                len(books),
            )
            return 0

        sent = 0
        bot = Bot(token=self.token)

        for start in range(0, len(books), self.max_per_message):
            chunk = books[start : start + self.max_per_message]
            text = render_digest(chunk, offset=start)

            try:
                await bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
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
        if not self.configured:
            log.info("telegram not configured; health digest not sent")
            return False
        try:
            await Bot(token=self.token).send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            log.error("telegram health digest failed: %s", exc)
            return False
        return True


def render_digest(books: list[ProcessedBook], offset: int = 0) -> str:
    header = (
        f"<b>{len(books)} new book{'s' if len(books) != 1 else ''} at Old Book Depot</b>"
    )
    return "\n\n".join([header, *(render_book(b, offset + i + 1) for i, b in enumerate(books))])


def render_book(book: ProcessedBook, rank: int) -> str:
    product = book.product
    facts = book.facts
    name = html.escape(product.work_title)
    price = f"₹{product.price_inr:.0f}" if product.price_inr else "price unknown"

    line = f"<b>{rank}. {name}</b>"
    # The shop omits the author on ~half its listings. Where the title did not
    # carry it either, enrichment usually learned it from the rating source.
    if author := (product.author or facts.resolved_author):
        line += f"\n<i>{html.escape(author)}</i>"

    bits = [price]
    if product.condition:
        bits.append(product.condition)
    if facts.has_rating:
        bits.append(f"{facts.rating}★ ({facts.ratings_count:,})")
    line += "\n" + " · ".join(bits)

    if tags := facts.flat_tags[:5]:
        line += "\n<i>" + " · ".join(html.escape(t.replace("-", " ")) for t in tags) + "</i>"

    if seen_before := _previously_seen(book):
        line += f"\n{seen_before}"

    if verdict := _value_verdict(book):
        line += f"\n{verdict}"

    if book.insights.blurb:
        line += f"\n\n{html.escape(book.insights.blurb)}"

    if product.permalink:
        line += f'\n\n<a href="{html.escape(product.permalink)}">buy</a>'

    line += f"  ·  score {book.breakdown.score:.2f}"
    if book.breakdown.confidence < 0.5:
        line += " (thin evidence)"
    return line


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
        f"↓ ₹{(previous - price) / 100:.0f} cheaper than when last listed "
        f"(₹{previous / 100:.0f})"
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

    if indian and indian.has_price and price:
        baseline = indian.price_inr
        shop = price / 100
        source = (indian.source or "india").replace("searxng:", "")
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
