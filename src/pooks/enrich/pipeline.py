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
from pooks.enrich import abebooks, googlebooks, hardcover, observations, openlibrary, quality
from pooks.enrich.http import PoliteClient
from pooks.enrich.indian_prices import fetch_indian_price
from pooks.enrich.match import MatchMethod
from pooks.enrich.ratings import RatingResolver
from pooks.enrich.searxng import SearxngClient
from pooks.enrich.sources import BookFacts, IndianPrice, ScarcitySignal, round_rating
from pooks.models import Product

log = logging.getLogger(__name__)


class Enricher:
    def __init__(self, config: Config, *, profile: str | None = None) -> None:
        """`profile` selects a named subset of sources from [backfill.<name>].

        The "fast" profile exists for the first pass over a cold catalogue: it
        drops Goodreads and Amazon, which are paced at 60s and 90s per request
        and account for nearly all of the ~57s/book a full pass costs. What it
        writes is low quality by construction, which is fine — every value it
        stores is non-primary, so the repair pass upgrades it later.
        """
        self.config = config
        self.profile = config.backfill.get(profile, {}) if profile else {}
        secrets = config.secrets
        self.searxng = SearxngClient(secrets.searxng_url)
        self.google_books_key = secrets.google_books_api_key
        self.resolver = RatingResolver(
            chain=self.profile.get("ratings_chain") or config.rating_chain,
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
        cached = store.get_enrichment(book_key) if store is not None else None

        if cached is not None and not force and not _is_expired(cached):
            return facts_from_row(book_key, cached), True

        facts, observed = await self._fetch_fresh(client, product, book_key)

        if store is not None:
            with transaction(store.conn):
                # Keyed by source, so this run can only add to what earlier runs
                # learned — and the projection below then reads the whole set.
                store.put_observations(
                    book_key, [(o.field, o.source, o.encode()) for o in observed]
                )
            # What replaced the per-field merge. A refetch happens precisely
            # when the last attempt was degraded, so the source may still be
            # throttled and the new answer can be worse; choosing from every
            # answer ever given makes that safe without a rule per field.
            facts = observations.project(
                observations.Ledger.from_rows(store.observations(book_key)),
                facts,
                self.resolver.chain,
                self.resolver.floors(),
            )

        facts.in_print = self._infer_in_print(facts, facts.provenance)

        if store is not None:
            persist(store, facts, chain=self.resolver.chain, attempts=_attempts(cached))
        return facts, False

    async def refresh_tags(
        self, client: PoliteClient, product: Product, *, store: Store
    ) -> dict[str, list[str]] | None:
        """Fill a missing tag list without re-asking anything else.

        The repair pass reaches this only when tags are a book's sole gap, which
        by construction means its rating and price already came from the primary
        sources. A full re-enrich would then spend Goodreads' 60s and Amazon's
        90s producing values the projection is guaranteed to discard, to obtain one
        Hardcover call paced at a second.
        """
        tags = await self._fetch_tags(client, product.isbn)
        with transaction(store.conn):
            store.put_tags(product.book_key, tags)
        return tags

    async def _fetch_fresh(
        self, client: PoliteClient, product: Product, book_key: str
    ) -> tuple[BookFacts, list[observations.Observation]]:
        """Facts as this run found them, plus what each source actually said.

        The second value is returned rather than kept on `self` because one
        `Enricher` serves a whole batch: per-book state on the instance would
        leak one book's answers onto the next.
        """
        title = product.work_title
        author = product.author
        isbn = product.isbn

        facts = BookFacts(
            book_key=book_key,
            isbn=isbn,
            match_method=(MatchMethod.ISBN if isbn else MatchMethod.FUZZY).value,
        )

        rating, provenance, obtained = await self.resolver.resolve(
            client, isbn=isbn, title=title, author=author
        )
        facts.provenance = provenance
        observed = [observations.rating_observation(r) for r in obtained]

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

        # Last resort for the synopsis, and it does not need an ISBN — which
        # matters, because Google Books does and the books without one are
        # exactly those most likely to be missing a description. Half the
        # enriched books had none at all, and a blurb with nothing to ground it
        # just restates the rating the card already shows.
        if not facts.synopsis:
            if description := await openlibrary.fetch_description(
                client, isbn=isbn, title=title, author=author
            ):
                facts.synopsis = description
                provenance["synopsis_source"] = "open_library"

        # Unconditional, not part of the rating chain. Hardcover sits *second*
        # there, so whenever Goodreads answers — the common case — it is never
        # queried and no tags would ever arrive.
        facts.tags = await self._fetch_tags(client, isbn)

        facts.scarcity = await self._fetch_scarcity(client, isbn)
        # The resolved title is preferred for the price identity check: it comes
        # from a rating source and is cleaner than the shop's listing text.
        facts.indian_price = await self._fetch_indian_price(
            client,
            isbn,
            title=facts.resolved_title or title,
            author=facts.resolved_author or author,
        )
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

        # Whatever else was learned along the way, attributed to whoever said
        # it. `synopsis_source` is set by the two backfill legs below; when it
        # is absent the text rode in on the accepted rating.
        if facts.synopsis:
            source = provenance.get("synopsis_source") or facts.rating_source
            if source:
                observed.append(observations.synopsis_observation(str(source), facts.synopsis))
        if facts.tags is not None:
            observed.append(observations.tags_observation(hardcover.SOURCE, facts.tags))
        if facts.scarcity is not None:
            observed.append(observations.scarcity_observation("abebooks", facts.scarcity))
        if (price := facts.indian_price) is not None and price.source:
            observed.append(observations.price_observation(price.source, price))

        return facts, observed

    async def _fetch_tags(
        self, client: PoliteClient, isbn: str | None
    ) -> dict[str, list[str]] | None:
        if not isbn:
            # Hardcover is looked up by ISBN, so without one there is nothing to
            # ask — settled, not pending. Returning None would mark the book
            # improvable forever and have the repair pass retry a lookup that
            # cannot be made.
            return {}
        return await hardcover.fetch_tags(client, isbn, self.config.secrets.hardcover_api_key)

    async def _fetch_scarcity(
        self, client: PoliteClient, isbn: str | None
    ) -> ScarcitySignal | None:
        if not isbn:
            return None
        enabled = self.profile.get("abebooks", self.config.prices.get("abebooks_enabled", True))
        if not enabled:
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
        sources = self.profile.get("india_sources", india.get("sources", []))
        if not india.get("enabled", True) or not sources:
            return None
        return await fetch_indian_price(
            client,
            isbn,
            title=title,
            author=author,
            searxng=self.searxng,
            sources=tuple(sources),
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


# Cache lifetimes, chosen by how good the answer was — not merely whether one
# exists. An earlier version returned "never expires" the moment a rating was
# present and never looked at the price at all, so a book enriched while Amazon
# was throttled kept an empty price permanently, and a rating from Open Library
# (too noisy to rank on) was as durable as one from Goodreads. Five of nine rows
# in the first real database were frozen that way.
TTL_DEGRADED = timedelta(minutes=30)
TTL_IMPROVABLE = timedelta(days=3)
TTL_GENUINE_MISS = timedelta(days=30)
TTL_EXHAUSTED = timedelta(days=30)


def _expiry_for(facts: BookFacts, chain: list[str], attempts: int = 0) -> str | None:
    """When this record should be reconsidered, or None to keep it forever."""
    if attempts >= quality.MAX_REFRESH_ATTEMPTS:
        return _in(TTL_EXHAUSTED)

    price = facts.indian_price
    degraded = bool(facts.provenance.get("degraded_hosts"))

    if degraded or (price is not None and price.unknown):
        return _in(TTL_DEGRADED)

    tier = quality.rating_tier(facts.rating_source, chain)
    if tier is None:
        # Every source answered and none had it. Real, but worth re-checking
        # occasionally — books do get added to Goodreads.
        return _in(TTL_GENUINE_MISS)
    if tier > 0:
        return _in(TTL_IMPROVABLE)

    price_tier = quality.price_tier(price.source if price else None)
    if price_tier is not None and price_tier > 0:
        return _in(TTL_IMPROVABLE)
    if price is None or (not price.has_price and not price.available_in_india):
        # "Not sold in India" is a complete answer; never having determined it
        # is not.
        return None if price is not None else _in(TTL_IMPROVABLE)

    return None


def _in(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat(timespec="seconds")


def _attempts(cached: Row | None) -> int:
    if cached is None:
        return 0
    try:
        return int(cached["refresh_attempts"] or 0)
    except (IndexError, KeyError, TypeError):
        return 0


def _is_expired(row: Row) -> bool:
    expires_at = row["expires_at"]
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(UTC)
    except ValueError:
        return True


def persist(store: Store, facts: BookFacts, *, chain: list[str], attempts: int = 0) -> None:
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
                "in_available": int(price.available_in_india) if price else None,
                "in_price_unknown": int(price.unknown) if price else None,
                "tags_json": None if facts.tags is None else json.dumps(facts.tags),
                "synopsis": facts.synopsis,
                "match_method": facts.match_method,
                "expires_at": _expiry_for(facts, chain, attempts),
                # Carried through, not incremented: `put_enrichment` overwrites
                # every column, so omitting it would reset the retry budget on
                # each ordinary re-enrich. Only `bump_refresh_attempt` counts.
                "refresh_attempts": attempts,
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
            listing_count=row["comp_listing_count"],
            has_new_offers=bool(row["scarcity_has_new"]),
        )

    indian_price = None
    if row["in_available"] is not None or row["in_price_paise"] is not None:
        indian_price = IndianPrice(
            price_paise=row["in_price_paise"],
            source=row["in_price_source"],
            available_in_india=bool(row["in_available"]),
            unknown=bool(row["in_price_unknown"]),
        )
    return BookFacts(
        book_key=book_key,
        isbn=row["isbn"],
        resolved_title=row["resolved_title"],
        resolved_author=row["resolved_author"],
        rating=None if row["rating"] is None else round_rating(row["rating"]),
        ratings_count=row["ratings_count"],
        rating_source=row["rating_source"],
        synopsis=row["synopsis"],
        in_print=None if row["in_print"] is None else bool(row["in_print"]),
        scarcity=scarcity,
        indian_price=indian_price,
        match_method=row["match_method"],
        tags=json.loads(row["tags_json"]) if row["tags_json"] is not None else None,
        provenance=json.loads(row["provenance_json"] or "{}"),
    )


def _dump(value: Any) -> str:
    return json.dumps(value, default=str)
