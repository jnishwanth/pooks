"""Domain models and the normalisation shared by ingest, enrichment and ranking."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


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


def normalise_isbn(value: str | None) -> str | None:
    """Return a bare ISBN-10/13, or None if it is not a plausible ISBN."""
    if not value:
        return None
    digits = _ISBN_CLEAN.sub("", value).upper()
    return digits if len(digits) in (10, 13) else None


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
    categories: list[str] = Field(default_factory=list)
    price_paise: int | None = None
    regular_price_paise: int | None = None
    in_stock: bool = False
    date_created: str | None = None
    date_modified: str | None = None

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
            raw = prices.get(key)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        pages_raw = attrs.get(ATTR_PAGES)
        try:
            pages = int(re.sub(r"[^0-9]", "", pages_raw)) if pages_raw else None
        except ValueError:
            pages = None

        return cls(
            product_id=payload["id"],
            name=clean_text(payload.get("name")) or f"product-{payload['id']}",
            slug=payload.get("slug"),
            permalink=payload.get("permalink"),
            isbn=normalise_isbn(attrs.get(ATTR_ISBN)),
            author=attrs.get(ATTR_AUTHOR) or None,
            publisher=attrs.get(ATTR_PUBLISHER) or None,
            book_format=attrs.get(ATTR_FORMAT) or None,
            pages=pages,
            condition=attrs.get(ATTR_CONDITION) or None,
            categories=[
                clean_text(c.get("name")) or "" for c in (payload.get("categories") or [])
            ],
            price_paise=paise("price"),
            regular_price_paise=paise("regular_price"),
            in_stock=bool(payload.get("is_in_stock")),
        )
