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
from pooks.enrich.pipeline import Enricher
from pooks.enrich.sources import BookFacts
from pooks.llm.client import LLMClient
from pooks.llm.pipeline import BookInsights, InsightGenerator
from pooks.models import NOTIFY_EVENTS, EventType, Product
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
) -> ProcessResult:
    result = ProcessResult()
    events = store.unprocessed_events(limit=limit)
    result.events_seen = len(events)
    if not events:
        return result

    enricher = Enricher(config)
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


async def rescore_in_stock(store: Store, config: Config, limit: int = 1000) -> int:
    """Recompute scores for everything in stock from cached data.

    Used after tuning weights in config.toml — reads only the cache, so it costs
    no API calls and no inference.
    """
    from pooks.enrich.pipeline import _facts_from_row
    from pooks.llm.pipeline import _from_cache

    rows = store.conn.execute(
        "SELECT * FROM products WHERE in_stock = 1 ORDER BY product_id DESC LIMIT ?", (limit,)
    ).fetchall()

    version = config.llm.get("prompt_version", 1)
    updated = 0

    # Drop scores whose enrichment has gone. Without this, a score computed
    # under an older scoring function lingers indefinitely for any book that is
    # no longer re-scored, and `top` and `calibrate` silently mix the two.
    with transaction(store.conn):
        stale = store.conn.execute(
            "DELETE FROM scores WHERE product_id IN ("
            "  SELECT s.product_id FROM scores s"
            "  JOIN products p ON p.product_id = s.product_id"
            "  LEFT JOIN enrichment e ON e.book_key = p.book_key"
            "  WHERE e.book_key IS NULL)"
        ).rowcount
    if stale:
        log.info("dropped %d score(s) with no enrichment behind them", stale)

    for row in rows:
        product = product_from_row(row)
        enrichment = store.get_enrichment(product.book_key)
        if enrichment is None:
            continue
        facts = _facts_from_row(product.book_key, enrichment)

        blurb = store.get_llm(product.book_key, "blurb", version)
        renown = store.get_llm(product.book_key, "renown", version)
        insights = _from_cache(blurb, renown) if blurb and renown else BookInsights()

        breakdown = score_book(product, facts, insights, config)
        with transaction(store.conn):
            store.put_score(product.product_id, breakdown.as_dict())
        updated += 1

    return updated


def event_is_silent(event_type: str) -> bool:
    return event_type in {str(EventType.SOLD_OUT), str(EventType.METADATA_CHANGE)}
