"""End-to-end orchestration: turn pending events into ranked, described books.

The cost policy from `ingest.diff` is honoured here rather than re-derived: an
event carries `requires_enrichment` and `requires_inference`, and this module
does exactly what they say. A sold-out event reaches this code and does nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from sqlite3 import Row

from pooks.config import Config
from pooks.db.store import Store, transaction
from pooks.enrich.http import PoliteClient
from pooks.enrich.pipeline import Enricher, facts_from_row
from pooks.enrich.sources import BookFacts
from pooks.llm.client import LLMClient
from pooks.llm.pipeline import BookInsights, InsightGenerator, insights_from_cache
from pooks.llm.roles import Role
from pooks.models import NOTIFY_EVENTS, Product
from pooks.rank.score import ScoreBreakdown, score_book

log = logging.getLogger(__name__)


@dataclass
class ProcessedBook:
    product: Product
    facts: BookFacts
    insights: BookInsights
    breakdown: ScoreBreakdown
    event_id: int
    event_type: str
    notify: bool
    # Cheapest price this book was listed at under a *previous* product id.
    # Relists get a new id, so this is the only place a price drop shows up.
    previous_price_paise: int | None = None


@dataclass
class ProcessResult:
    processed: list[ProcessedBook] = field(default_factory=list)
    events_seen: int = 0
    enriched: int = 0
    cache_hits: int = 0
    inferred: int = 0
    silent: int = 0

    @property
    def to_notify(self) -> list[ProcessedBook]:
        return sorted(
            (b for b in self.processed if b.notify),
            key=lambda b: b.breakdown.score,
            reverse=True,
        )


def product_from_row(row: Row) -> Product:
    return Product(
        product_id=row["product_id"],
        name=row["name"],
        slug=row["slug"],
        permalink=row["permalink"],
        isbn=row["isbn"],
        author=row["author"],
        publisher=row["publisher"],
        book_format=row["book_format"],
        pages=row["pages"],
        condition=row["condition"],
        categories=json.loads(row["categories_json"] or "[]"),
        price_paise=row["price_paise"],
        regular_price_paise=row["regular_price_paise"],
        in_stock=bool(row["in_stock"]),
        date_created=row["date_created"],
        date_modified=row["date_modified"],
    )


async def process_pending(
    store: Store,
    config: Config,
    *,
    limit: int = 50,
    dry_run: bool = False,
    profile: str | None = None,
) -> ProcessResult:
    result = ProcessResult()
    events = store.unprocessed_events(limit=limit)
    result.events_seen = len(events)
    if not events:
        return result

    enricher = Enricher(config, profile=profile)
    llm = LLMClient.from_config(config)
    insight_gen = InsightGenerator(llm, config.llm.get("prompt_version", 1))

    threshold = config.notify.get("push_score_threshold", 0.62)
    min_confidence = config.notify.get("push_min_confidence", 0.5)

    handled_event_ids: list[int] = []

    async with PoliteClient() as client:
        for event in events:
            handled_event_ids.append(event["id"])

            if not event["requires_enrichment"]:
                # SOLD_OUT and METADATA_CHANGE land here: recorded, nothing spent.
                result.silent += 1
                continue

            row = store.get_product(event["product_id"])
            if row is None:
                continue
            product = product_from_row(row)

            facts, from_cache = await enricher.enrich(client, product, store=store)
            result.enriched += 1
            result.cache_hits += int(from_cache)

            insights = BookInsights(skipped_reason="inference not required for this event")
            if event["requires_inference"]:
                insights = await insight_gen.generate(store, product, facts)
                if insights.skipped_reason is None:
                    result.inferred += 1

            breakdown = score_book(product, facts, insights, config)
            if not dry_run:
                with transaction(store.conn):
                    store.put_score(product.product_id, breakdown.as_dict())

            details = json.loads(event["details_json"] or "{}")
            notify = (
                event["event_type"] in {str(e) for e in NOTIFY_EVENTS}
                and not details.get("backfill")
                and breakdown.score >= threshold
                and breakdown.confidence >= min_confidence
                and not store.already_notified(product.product_id, event["id"])
            )

            result.processed.append(
                ProcessedBook(
                    product=product,
                    facts=facts,
                    insights=insights,
                    breakdown=breakdown,
                    event_id=event["id"],
                    event_type=event["event_type"],
                    notify=notify,
                    previous_price_paise=store.previous_price_paise(
                        product.book_key, product.product_id
                    ),
                )
            )

    if not dry_run:
        with transaction(store.conn):
            store.mark_events_processed(handled_event_ids)

    log.info(
        "processed %d events: %d enriched (%d cached), %d inferred, %d silent, %d to notify",
        result.events_seen,
        result.enriched,
        result.cache_hits,
        result.inferred,
        result.silent,
        len(result.to_notify),
    )
    return result


def load_cached(
    store: Store, config: Config, row: Row
) -> tuple[Product, BookFacts, BookInsights] | None:
    """Rebuild a book entirely from cache, or None if it has not been enriched.

    The single place that knows how to reassemble a book from stored rows.
    Two callers previously each repeated the sequence and reached across module
    boundaries for private helpers, which is how the cache and fresh paths drifted
    apart once already: a value that enrichment computed but did not persist made
    cached books score differently from freshly fetched ones.
    """
    product = product_from_row(row)
    enrichment = store.get_enrichment(product.book_key)
    if enrichment is None:
        return None

    facts = facts_from_row(product.book_key, enrichment)

    version = config.llm.get("prompt_version", 1)
    blurb = store.get_llm(product.book_key, Role.BLURB, version)
    renown = store.get_llm(product.book_key, Role.RENOWN, version)
    insights = insights_from_cache(blurb, renown) if blurb and renown else BookInsights()

    return product, facts, insights


@dataclass
class RefreshResult:
    attempted: int = 0
    improved: int = 0
    unchanged: int = 0


async def refresh_improvable(
    store: Store, config: Config, *, limit: int = 3
) -> RefreshResult:
    """Re-enrich books whose cached answer came from a fallback or a block.

    The anti-entropy half of the design: enrichment degrades gracefully when a
    source is throttled, and this is what stops that degradation being
    permanent. Without it a book enriched during an Amazon outage keeps an empty
    price forever, and a rating scraped from Open Library is never upgraded to
    Goodreads.

    Re-scoring afterwards is the point — an improved price changes the ranking.
    """
    from pooks.enrich.quality import assess, improvable

    result = RefreshResult()
    rows = store.improvable_books(
        limit,
        config.primary_rating_source,
        min_score=config.schedule.get("refresh_min_score", 0.0),
    )
    if not rows:
        return result

    enricher = Enricher(config)
    chain = config.rating_chain

    async with PoliteClient() as client:
        for row in rows:
            provenance = json.loads(row["provenance_json"] or "{}")
            can_improve, why = improvable(
                assess(row, chain, provenance), price_available=row["in_available"]
            )
            if not can_improve:
                continue

            product = product_from_row(row)
            before = (row["rating_source"], row["in_price_source"])
            result.attempted += 1

            # force=True: the record is unexpired by definition when the daemon
            # picks it, since selection is by quality rather than by age.
            facts, _ = await enricher.enrich(client, product, store=store, force=True)
            store.bump_refresh_attempt(product.book_key)

            after = (facts.rating_source, facts.indian_price.source if facts.indian_price else None)
            if after != before:
                result.improved += 1
                log.info(
                    "refreshed %s (%s): %s -> %s", product.work_title[:40], why, before, after
                )
            else:
                result.unchanged += 1

    if result.improved:
        await rescore_in_stock(store, config)
    return result


async def rescore_in_stock(store: Store, config: Config, limit: int = 1000) -> int:
    """Recompute scores for everything in stock from cached data.

    Used after tuning weights in config.toml — reads only the cache, so it costs
    no API calls and no inference.
    """
    rows = store.in_stock_products(limit)

    updated = 0

    with transaction(store.conn):
        stale = store.prune_unbacked_scores()
    if stale:
        log.info("dropped %d score(s) with no enrichment behind them", stale)

    for row in rows:
        cached = load_cached(store, config, row)
        if cached is None:
            continue
        product, facts, insights = cached

        breakdown = score_book(product, facts, insights, config)
        with transaction(store.conn):
            store.put_score(product.product_id, breakdown.as_dict())
        updated += 1

    return updated

