"""Enrichment orchestration.

Everything is keyed by `book_key` (ISBN where available), never by product id.
The shop relists the same titles constantly, so a book that has been enriched
once is free forever after — which is what makes a restock cost nothing.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from sqlite3 import Row
from typing import Any

from pooks.config import Config
from pooks.db.store import Store, transaction
from pooks.enrich import abebooks, googlebooks, openlibrary
from pooks.enrich.http import PoliteClient
from pooks.enrich.indian_prices import fetch_indian_price
from pooks.enrich.match import MatchMethod
from pooks.enrich.ratings import RatingResolver
from pooks.enrich.searxng import SearxngClient
from pooks.enrich.sources import BookFacts, IndianPrice, ScarcitySignal
from pooks.models import Product

log = logging.getLogger(__name__)


class Enricher:
    def __init__(self, config: Config) -> None:
        self.config = config
        secrets = config.secrets
        self.searxng = SearxngClient(secrets.searxng_url)
        self.google_books_key = secrets.google_books_api_key
        self.resolver = RatingResolver(
            chain=config.ratings.get("chain", []),
            min_ratings_count=config.ratings.get("min_ratings_count", 50),
            min_count_by_source=config.ratings.get("min_count_by_source", {}),
            searxng=self.searxng,
            hardcover_key=secrets.hardcover_api_key,
            google_books_key=self.google_books_key,
            accept_score=config.matching.get("fuzzy_accept_score", 92.0),
            reject_score=config.matching.get("fuzzy_reject_score", 70.0),
        )

    async def enrich(
        self,
        client: PoliteClient,
        product: Product,
        *,
        store: Store | None = None,
        force: bool = False,
    ) -> tuple[BookFacts, bool]:
        """Everything known about a product's underlying book.

        Returns `(facts, from_cache)`. The cache hit is the common path once the
        catalogue has been seen once, and is what makes a relisted book free.
        """
        book_key = product.book_key

        if store is not None and not force:
            cached = store.get_enrichment(book_key)
            if cached is not None and not _is_expired(cached):
                return facts_from_row(book_key, cached), True

        facts = await self._fetch_fresh(client, product, book_key)
        if store is not None:
            persist(store, facts)
        return facts, False

    async def _fetch_fresh(
        self, client: PoliteClient, product: Product, book_key: str
    ) -> BookFacts:
        title = product.work_title
        author = product.author
        isbn = product.isbn

        facts = BookFacts(
            book_key=book_key,
            isbn=isbn,
            match_method=(MatchMethod.ISBN if isbn else MatchMethod.FUZZY).value,
        )

        rating, provenance = await self.resolver.resolve(
            client, isbn=isbn, title=title, author=author
        )
        facts.provenance = provenance

        if rating is not None:
            facts.rating = rating.rating
            facts.ratings_count = rating.ratings_count
            facts.rating_source = rating.source
            facts.resolved_title = rating.title
            facts.resolved_author = rating.author
            facts.synopsis = rating.synopsis

        # The blurb generator must work from retrieved text, not model memory,
        # so a synopsis is fetched even when the rating came from elsewhere.
        if not facts.synopsis and isbn:
            volume = await googlebooks.fetch_volume_info(client, isbn, self.google_books_key)
            if volume:
                info = volume.get("volumeInfo") or {}
                facts.synopsis = info.get("description")
                facts.resolved_title = facts.resolved_title or info.get("title")
                provenance["synopsis_source"] = "google_books"
                if saleability := (volume.get("saleInfo") or {}).get("saleability"):
                    provenance["google_saleability"] = saleability

        facts.scarcity = await self._fetch_scarcity(client, isbn)
        # The resolved title is preferred for the price identity check: it comes
        # from a rating source and is cleaner than the shop's listing text.
        facts.indian_price = await self._fetch_indian_price(
            client,
            isbn,
            title=facts.resolved_title or title,
            author=facts.resolved_author or author,
        )
        facts.in_print = self._infer_in_print(facts, provenance)

        # Popularity proxy for books with no usable rating — Open Library shelf
        # counts exist far more often than its ratings do.
        if not facts.has_rating and isbn:
            if doc := await openlibrary.fetch_metadata(client, isbn):
                provenance["open_library_meta"] = {
                    "edition_count": doc.get("edition_count"),
                    "first_publish_year": doc.get("first_publish_year"),
                }
                if work_key := doc.get("key"):
                    if want := await openlibrary.fetch_want_to_read(client, work_key):
                        provenance["want_to_read"] = want

        facts.resolved_title = facts.resolved_title or title
        facts.resolved_author = facts.resolved_author or author
        return facts

    async def _fetch_scarcity(
        self, client: PoliteClient, isbn: str | None
    ) -> ScarcitySignal | None:
        if not isbn or not self.config.prices.get("abebooks_enabled", True):
            return None
        return await abebooks.fetch_scarcity(
            client, isbn, max_listings=self.config.prices.get("max_comp_listings", 20)
        )

    async def _fetch_indian_price(
        self,
        client: PoliteClient,
        isbn: str | None,
        *,
        title: str | None,
        author: str | None,
    ) -> IndianPrice | None:
        if not isbn:
            return None
        india = self.config.prices.get("india", {})
        if not india.get("enabled", True):
            return None
        return await fetch_indian_price(
            client,
            isbn,
            title=title,
            author=author,
            searxng=self.searxng,
            sources=tuple(india.get("sources", ("amazon", "retailers", "searxng"))),
        )

    def _infer_in_print(self, facts: BookFacts, provenance: dict[str, Any]) -> bool | None:
        """Best-effort in-print flag.

        For a shop full of out-of-print Pantheon and OUP editions this often
        matters more than price: when nothing is available new, "cheap" and
        "scarce" are the same verdict.

        New copies on the international used market is the strongest available
        signal. An Indian retailer stocking it corroborates. Google Books
        saleability is weakest — it reflects Play Books, i.e. an ebook rather
        than the print edition.
        """
        scarcity = facts.scarcity
        if scarcity and scarcity.has_new_offers:
            provenance["in_print_signal"] = "abebooks new offers"
            return True
        if facts.indian_price and facts.indian_price.has_price:
            provenance["in_print_signal"] = "stocked by an indian retailer"
            return True
        if provenance.get("google_saleability") == "FOR_SALE":
            provenance["in_print_signal"] = "google play saleability (weak)"
            return True
        if scarcity and scarcity.has_data:
            provenance["in_print_signal"] = "used offers only"
            return False
        return None


# Cache lifetimes, chosen by how trustworthy the answer is. A rating is stable
# so it never expires; a genuine miss is worth re-checking occasionally; a miss
# caused by a blocked source must be retried soon, or one throttling episode
# would permanently mark those books as unrated.
TTL_DEGRADED = timedelta(minutes=30)
TTL_GENUINE_MISS = timedelta(days=30)


def _expiry_for(facts: BookFacts) -> str | None:
    if facts.has_rating:
        return None
    degraded = facts.provenance.get("degraded_hosts")
    ttl = TTL_DEGRADED if degraded else TTL_GENUINE_MISS
    return (datetime.now(UTC) + ttl).isoformat(timespec="seconds")


def _is_expired(row: Row) -> bool:
    expires_at = row["expires_at"]
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(UTC)
    except ValueError:
        return True


def persist(store: Store, facts: BookFacts) -> None:
    scarcity = facts.scarcity
    price = facts.indian_price
    with transaction(store.conn):
        store.put_enrichment(
            facts.book_key,
            {
                "isbn": facts.isbn,
                "resolved_title": facts.resolved_title,
                "resolved_author": facts.resolved_author,
                "rating": facts.rating,
                "ratings_count": facts.ratings_count,
                "rating_source": facts.rating_source,
                "provenance_json": _dump(facts.provenance),
                "in_print": None if facts.in_print is None else int(facts.in_print),
                "comp_listing_count": scarcity.listing_count if scarcity else None,
                "scarcity_has_new": int(scarcity.has_new_offers) if scarcity else None,
                "in_price_paise": price.price_paise if price else None,
                "in_price_source": price.source if price else None,
                "in_price_url": price.url if price else None,
                "in_available": int(price.available_in_india) if price else None,
                "in_price_unknown": int(price.unknown) if price else None,
                "synopsis": facts.synopsis,
                "match_method": facts.match_method,
                "expires_at": _expiry_for(facts),
            },
        )


def facts_from_row(book_key: str, row: Row) -> BookFacts:
    """Rebuild facts from cache.

    Every field the scorer reads must be restored here. A value computed during
    enrichment but not round-tripped silently degrades every cached read while
    fresh runs look correct — which is exactly how the landed-cost bug hid.
    `tests/test_cache_roundtrip.py` asserts fresh and cached facts score
    identically for that reason.
    """
    scarcity = None
    if row["comp_listing_count"] is not None:
        scarcity = ScarcitySignal(
            source="abebooks",
            listing_count=row["comp_listing_count"],
            has_new_offers=bool(row["scarcity_has_new"]),
        )

    indian_price = None
    if row["in_available"] is not None or row["in_price_paise"] is not None:
        indian_price = IndianPrice(
            price_paise=row["in_price_paise"],
            source=row["in_price_source"],
            url=row["in_price_url"],
            available_in_india=bool(row["in_available"]),
            unknown=bool(row["in_price_unknown"]),
        )
    return BookFacts(
        book_key=book_key,
        isbn=row["isbn"],
        resolved_title=row["resolved_title"],
        resolved_author=row["resolved_author"],
        rating=row["rating"],
        ratings_count=row["ratings_count"],
        rating_source=row["rating_source"],
        synopsis=row["synopsis"],
        in_print=None if row["in_print"] is None else bool(row["in_print"]),
        scarcity=scarcity,
        indian_price=indian_price,
        match_method=row["match_method"],
        provenance=json.loads(row["provenance_json"] or "{}"),
    )


def _dump(value: Any) -> str:
    return json.dumps(value, default=str)
