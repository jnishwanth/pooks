"""Domain models and the normalisation shared by ingest, enrichment and ranking."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from selectolax.parser import HTMLParser


class EventType(StrEnum):
    NEW_IN_STOCK = "NEW_IN_STOCK"
    BACK_IN_STOCK = "BACK_IN_STOCK"
    PRICE_CHANGE = "PRICE_CHANGE"
    SOLD_OUT = "SOLD_OUT"
    METADATA_CHANGE = "METADATA_CHANGE"


# The cost policy, declared as data. A SOLD_OUT delta must reach neither the
# enrichment layer nor the LLM: it updates the DB and stays silent.
ENRICH_EVENTS = frozenset({EventType.NEW_IN_STOCK, EventType.BACK_IN_STOCK, EventType.PRICE_CHANGE})
INFERENCE_EVENTS = frozenset({EventType.NEW_IN_STOCK})
NOTIFY_EVENTS = frozenset({EventType.NEW_IN_STOCK, EventType.BACK_IN_STOCK})


def notifiable(event_type: str, *, backfill: bool) -> bool:
    """Whether a change of this kind is worth a push at all.

    The rule is needed in two places — at classification, where the diff reports
    what a sweep would push, and in `run.process_pending`, where the push
    actually happens from a stored event row — so it lives beside the set it
    reads rather than being spelled out at both.

    `EventType` is a `StrEnum`, whose MRO puts `str` ahead of `Enum` — members
    therefore hash and compare as their *value*, not by identity or by name, so
    the stored `event_type` string finds its member in the set. Taking it as
    `str` is what lets the second caller pass a database column without
    rebuilding the set from it. Demoting `EventType` to a plain `Enum` would
    silently stop every push instead of failing; `tests/test_models.py` pins the
    lookup for that reason.
    """
    return not backfill and event_type in NOTIFY_EVENTS


# Attribute names as they appear in the WooCommerce Store API payload.
ATTR_AUTHOR = "Author"
ATTR_ISBN = "ISBN"
ATTR_PUBLISHER = "Publisher"
ATTR_FORMAT = "Format"
ATTR_PAGES = "Pages"
ATTR_CONDITION = "Book Condition"

_ISBN_CLEAN = re.compile(r"[^0-9Xx]")
_PAREN = re.compile(r"\s*[\(\[][^)\]]*[)\]]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Shop titles embed the author: "Some Title by Jane Doe (Penguin Classics)".
_BY_AUTHOR = re.compile(r"\s+by\s+.+$", re.IGNORECASE)
# Same pattern, capturing the author rather than discarding it.
_BY_AUTHOR_CAPTURE = re.compile(r"\s+by\s+(.+)$", re.IGNORECASE)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clean_text(value: str | None) -> str | None:
    """Unescape HTML entities and collapse whitespace.

    The Store API returns entity-encoded text (categories arrive as
    'Literature &amp; Fiction'), which corrupts search queries if passed through.
    """
    if not value:
        return None
    text = html.unescape(value)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def html_to_text(value: str | None) -> str | None:
    """Readable text from a block of markup, or None if it carries none.

    The shop writes its product description as real HTML — paragraphs, entities,
    and on 117 of 574 in-stock listings the wrapper divs of the chat UI it was
    pasted from (`<div class="group/ai-message-item">`). A regex strip leaves
    those class names in the text, so it goes through the parser `indian_prices`
    already uses rather than a second, worse one.

    Markup carrying no words — `<p></p>` — is None rather than the empty string,
    for the same reason `clean_text` is: "the shop has no description" and "the
    shop has an empty one" are not different facts to anything downstream.
    """
    if not value:
        return None
    return clean_text(HTMLParser(value).text(separator=" "))


def normalise_isbn(value: str | None) -> str | None:
    """Return a bare ISBN-10/13, or None if it is not a plausible ISBN."""
    if not value:
        return None
    digits = _ISBN_CLEAN.sub("", value).upper()
    return digits if len(digits) in (10, 13) else None


def author_from_title(title: str) -> str | None:
    """Pull the author out of a "... by <Author>" title.

    The shop leaves the `Author` attribute unset on roughly half its in-stock
    catalogue — verified against the live API, which returns Book Condition,
    ISBN, Publisher and Pages for such products and no author at all. It is
    almost always in the title anyway: 293 of 324 untagged books carry it as a
    suffix, so harvesting it takes coverage from 49% to ~95%.

    `strip_title` already locates this suffix in order to remove it; this keeps
    what it discards.
    """
    cleaned = clean_text(title) or ""
    match = _BY_AUTHOR_CAPTURE.search(cleaned)
    if not match:
        return None

    # Drop a trailing edition note: "by Amarpal Singh (Hardcover)".
    author = _PAREN.sub("", match.group(1)).strip(" -–—:,.")
    if not author or len(author) > 60:
        return None
    # A lone initial or a stray word is noise rather than an author.
    return author if len(author) > 2 else None


def strip_title(title: str) -> str:
    """Reduce a shop title to the work title.

    'This Way for the Gas, Ladies and Gentlemen by Tadeusz Borowski (Penguin
    Classics)' -> 'This Way for the Gas, Ladies and Gentlemen'. Used for search
    queries and fuzzy matching, where the trailing author and edition note are
    noise that suppresses match scores.
    """
    cleaned = clean_text(title) or ""
    cleaned = _PAREN.sub("", cleaned)
    cleaned = _BY_AUTHOR.sub("", cleaned)
    return cleaned.strip(" -–—:,") or (clean_text(title) or "")


def slugify(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", clean_text(value) or "").encode("ascii", "ignore").decode()
    return _NON_ALNUM.sub("-", text.lower()).strip("-")


def first_image(payload: dict[str, Any]) -> str | None:
    """The shop's own photograph of a listing, from a Store API payload.

    Every in-stock listing carries one (574/574 measured 2026-09-03) because the
    shop photographs the actual copy rather than reusing a stock cover. The read
    is guarded rather than subscripted all the same: a missing key, an empty
    list and an entry without a `src` all have to mean "no cover" rather than
    an exception on the ingest path.
    """
    images = payload.get("images") or []
    if not images:
        return None
    src: str = (images[0].get("src") or "").strip()
    # Absolute http(s) only. This URL is handed to Telegram as a link preview,
    # and a malformed one is answered with a 400 that drops the entire message
    # — every book in it, permanently, since their events are already marked
    # processed by then. Storing nothing is strictly better than storing that.
    return src if src.startswith(("https://", "http://")) else None


def make_book_key(isbn: str | None, title: str, author: str | None) -> str:
    """Stable identity for enrichment caching.

    Prefers ISBN (present on ~93% of in-stock items). Falls back to a
    title+author slug so the ~7% without one still cache, accepting that two
    listings whose titles differ slightly will not share a cache entry.
    """
    if isbn := normalise_isbn(isbn):
        return f"isbn:{isbn}"
    return f"ta:{slugify(strip_title(title))}|{slugify(author)}"


class Product(BaseModel):
    """A shop listing, flattened from the Store API payload."""

    product_id: int
    name: str
    slug: str | None = None
    permalink: str | None = None
    isbn: str | None = None
    author: str | None = None
    publisher: str | None = None
    book_format: str | None = None
    pages: int | None = None
    condition: str | None = None
    # The shop's own copy for this listing, as plain text. Not evidence: it is
    # marketing prose the shop wrote, present on ~99% of listings, so scoring it
    # would add the same constant to everything. See ADR 18.
    description: str | None = None
    categories: list[str] = Field(default_factory=list)
    price_paise: int | None = None
    regular_price_paise: int | None = None
    in_stock: bool = False
    date_created: str | None = None
    date_modified: str | None = None
    # The shop's own photograph of this copy. Presentation only — it is not
    # evidence and never reaches a score.
    image_url: str | None = None

    @property
    def book_key(self) -> str:
        return make_book_key(self.isbn, self.name, self.author)

    @property
    def work_title(self) -> str:
        return strip_title(self.name)

    @property
    def price_inr(self) -> float | None:
        return None if self.price_paise is None else self.price_paise / 100

    @classmethod
    def from_store_api(cls, payload: dict[str, Any]) -> Product:
        attrs: dict[str, str] = {}
        for attribute in payload.get("attributes") or []:
            terms = attribute.get("terms") or []
            if attribute.get("name") and terms:
                attrs[attribute["name"]] = clean_text(terms[0].get("name")) or ""

        prices = payload.get("prices") or {}

        def paise(key: str) -> int | None:
            raw: Any = prices.get(key)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        pages_raw = attrs.get(ATTR_PAGES)
        try:
            pages = int(re.sub(r"[^0-9]", "", pages_raw)) if pages_raw else None
        except ValueError:
            pages = None

        name = clean_text(payload.get("name")) or f"product-{payload['id']}"

        return cls(
            product_id=payload["id"],
            name=name,
            slug=payload.get("slug"),
            permalink=payload.get("permalink"),
            isbn=normalise_isbn(attrs.get(ATTR_ISBN)),
            # The attribute is missing on ~51% of in-stock listings; the title
            # carries it for 90% of those.
            author=attrs.get(ATTR_AUTHOR) or author_from_title(name),
            publisher=attrs.get(ATTR_PUBLISHER) or None,
            book_format=attrs.get(ATTR_FORMAT) or None,
            pages=pages,
            condition=attrs.get(ATTR_CONDITION) or None,
            # Already in every poll and sweep body — the Store API request sets
            # no `_fields` filter, so this costs no extra request.
            description=html_to_text(payload.get("description")),
            categories=[clean_text(c.get("name")) or "" for c in (payload.get("categories") or [])],
            price_paise=paise("price"),
            regular_price_paise=paise("regular_price"),
            in_stock=bool(payload.get("is_in_stock")),
            image_url=first_image(payload),
        )
