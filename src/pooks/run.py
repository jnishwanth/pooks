"""End-to-end orchestration: turn pending events into ranked, described books.

The cost policy from `ingest.diff` is honoured here rather than re-derived: an
event carries `requires_enrichment` and `requires_inference`, and this module
does exactly what they say. A sold-out event reaches this code and does nothing.

Whether to push is the one decision the event cannot carry, because it also
depends on a score that does not exist yet when the event is recorded. Both
halves of that decision are applied below through their owning predicates —
`models.notifiable` for the kind of change, `rank.score.pushable` for the score
it earned — rather than a second spelling of either rule.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from sqlite3 import Row

from pooks.config import Config
from pooks.db.store import Store, product_from_row, transaction
from pooks.enrich.http import PoliteClient
from pooks.enrich.pipeline import Enricher, facts_from_row
from pooks.enrich.sources import BookFacts
from pooks.llm.client import LLMClient
from pooks.llm.pipeline import BookInsights, InsightGenerator, insights_from_cache
from pooks.llm.roles import Role
from pooks.models import Product, notifiable
from pooks.rank.score import ScoreBreakdown, pushable, score_book

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
    insight_gen = InsightGenerator(llm, config.prompt_version)

    threshold = config.push_score_threshold
    min_confidence = config.push_min_confidence

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
                notifiable(event["event_type"], backfill=bool(details.get("backfill")))
                and pushable(
                    breakdown.score,
                    breakdown.confidence,
                    threshold=threshold,
                    min_confidence=min_confidence,
                )
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

    version = config.prompt_version
    blurb = store.get_llm(product.book_key, Role.BLURB, version)
    renown = store.get_llm(product.book_key, Role.RENOWN, version)
    insights = insights_from_cache(blurb, renown) if blurb and renown else BookInsights()

    return product, facts, insights


def ranked_cached(
    store: Store, config: Config, *, limit: int | None = None
) -> Iterator[tuple[Row, Product, BookFacts, BookInsights]]:
    """Best-ranked in-stock books that can be rebuilt from cache, in rank order.

    Unscored books are skipped because they have not been through the pipeline
    yet, and un-enriched ones because there is nothing to rebuild them from.
    `pooks blurbs` and `pooks notify` both want exactly that set and each
    repeated the pair of skips around `load_cached`; the row is yielded too,
    since the score columns it carries are why the caller asked for the ranking.
    """
    for row in store.ranked_in_stock(limit=limit):
        if row["score"] is None:
            continue
        if (cached := load_cached(store, config, row)) is not None:
            yield row, *cached


@dataclass
class BlurbCandidates:
    """Ranked books that could have a blurb and do not.

    `ungrounded` is reported rather than silently dropped because it is the
    actionable number: a book with no synopsis is not waiting on an LLM call,
    it is waiting on `pooks refresh` finding it some retrieved text.
    """

    ready: list[tuple[Product, BookFacts]] = field(default_factory=list)
    ungrounded: int = 0


def blurb_candidates(store: Store, config: Config, *, scan: int | None = None) -> BlurbCandidates:
    """Books in the top `scan` of the ranking that still need a blurb.

    `scan` bounds how deep into the ranking to look, not how many to return,
    and the two callers want opposite things from that. `pooks blurbs --top N`
    means *the top N books*, so running it twice is a no-op rather than a walk
    deeper into the ranking. The daemon passes None and takes a few per tick, so
    over days it covers the catalogue best-first.

    Books with nothing to ground a blurb are counted, not returned: generation
    from no retrieved text pads with metadata the card already shows.
    """
    candidates = BlurbCandidates()
    for _, product, facts, insights in ranked_cached(store, config, limit=scan):
        if insights.blurb:
            continue
        if not (facts.synopsis or "").strip():
            candidates.ungrounded += 1
            continue
        candidates.ready.append((product, facts))
    return candidates


@dataclass
class RefreshResult:
    attempted: int = 0
    improved: int = 0
    unchanged: int = 0


async def refresh_improvable(store: Store, config: Config, *, limit: int = 3) -> RefreshResult:
    """Re-enrich books whose cached answer came from a fallback or a block.

    The anti-entropy half of the design: enrichment degrades gracefully when a
    source is throttled, and this is what stops that degradation being
    permanent. Without it a book enriched during an Amazon outage keeps an empty
    price forever, and a rating scraped from Open Library is never upgraded to
    Goodreads.

    Two repair actions, because the reason decides what is worth spending: a
    book whose only gap is its tags already has a primary rating and price, so
    it gets the one Hardcover call it needs instead of the whole chain. A book
    below `refresh_min_score` gets that same call whatever else is wrong with
    it — the selection offered it up for its tags alone, and honouring that
    here is what stops the cheap branch smuggling a 90s lookup past the floor.

    Re-scoring is what makes the expensive path worth taking — an improved price
    changes the ranking — so only that path triggers it. Tags are a filter, not
    a scoring input, and rebuilding every score to record one would be work the
    ranking cannot see.
    """
    from pooks.enrich.quality import TAGS_UNASKED, assess, improvable

    result = RefreshResult()
    rows = store.improvable_books(
        config.primary_rating_source,
        tags_askable=config.tags_askable,
        limit=limit,
        min_score=config.refresh_min_score,
    )
    if not rows:
        return result

    enricher = Enricher(config)
    chain = config.rating_chain
    scores_stale = False

    async with PoliteClient() as client:
        for row in rows:
            provenance = json.loads(row["provenance_json"] or "{}")
            can_improve, why = improvable(
                assess(row, chain, provenance), price_available=row["in_available"]
            )
            if not can_improve:
                continue

            product = product_from_row(row)
            sources_before = (row["rating_source"], row["in_price_source"])
            tagged_before = row["tags_json"] is not None
            result.attempted += 1

            if why == TAGS_UNASKED or not row["full_refresh_ok"]:
                tags = await enricher.refresh_tags(client, product, store=store)
                sources_after = sources_before
                tagged_after = tags is not None
            else:
                # force=True: the record is unexpired by definition when the daemon
                # picks it, since selection is by quality rather than by age.
                facts, _ = await enricher.enrich(client, product, store=store, force=True)
                sources_after = (
                    facts.rating_source,
                    facts.indian_price.source if facts.indian_price else None,
                )
                tagged_after = facts.tags is not None
            with transaction(store.conn):
                store.bump_refresh_attempt(product.book_key)

            if (sources_after, tagged_after) != (sources_before, tagged_before):
                result.improved += 1
                scores_stale = scores_stale or sources_after != sources_before
                log.info(
                    "refreshed %s (%s): %s -> %s",
                    product.work_title[:40],
                    why,
                    (*sources_before, tagged_before),
                    (*sources_after, tagged_after),
                )
            else:
                result.unchanged += 1

    if scores_stale:
        await rescore_in_stock(store, config)
    return result


async def rescore_in_stock(store: Store, config: Config) -> int:
    """Recompute scores for everything in stock from cached data.

    Used after tuning weights in config.toml — reads only the cache, so it costs
    no API calls and no inference. Deliberately unbounded: a partial rescore
    leaves the catalogue mixing two scoring functions, which is what
    `prune_unbacked_scores` below exists to prevent.
    """
    rows = store.in_stock_products()

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
