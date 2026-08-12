"""Change detection and event classification.

This module decides what a change *costs*. The rule that motivates the split:
a book going out of stock must update the database and stay silent — no
enrichment, no inference, no notification. Only genuinely new information is
allowed to reach the expensive layers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from sqlite3 import Row
from typing import Any

from pooks.db.store import Store
from pooks.models import (
    ENRICH_EVENTS,
    INFERENCE_EVENTS,
    EventType,
    Product,
    notifiable,
)

log = logging.getLogger(__name__)

# Fields whose change is recorded but never triggers work.
METADATA_FIELDS = ("name", "author", "publisher", "book_format", "pages", "condition", "isbn")


@dataclass
class DetectedEvent:
    product_id: int
    event_type: EventType
    details: dict[str, Any] = field(default_factory=dict)
    # Set on the first sweep into an empty database. The whole catalogue looks
    # "new" then, but it is not: inferring on all ~634 in-stock books would cost
    # thousands of LLM calls and push hundreds of notifications for books that
    # have been sitting on the shelf for weeks. Enrichment still runs (it warms
    # the cache); inference and notification do not.
    backfill: bool = False

    @property
    def requires_enrichment(self) -> bool:
        return self.event_type in ENRICH_EVENTS

    @property
    def requires_inference(self) -> bool:
        return not self.backfill and self.event_type in INFERENCE_EVENTS

    @property
    def should_notify(self) -> bool:
        return notifiable(self.event_type, backfill=self.backfill)


@dataclass
class DiffResult:
    events: list[DetectedEvent] = field(default_factory=list)
    seen_product_ids: set[int] = field(default_factory=set)
    backfill: bool = False

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[str(event.event_type)] = counts.get(str(event.event_type), 0) + 1
        return counts

    @property
    def enrichment_count(self) -> int:
        return sum(1 for e in self.events if e.requires_enrichment)

    @property
    def inference_count(self) -> int:
        return sum(1 for e in self.events if e.requires_inference)


def classify(
    products: list[Product],
    store: Store,
    *,
    full_sweep: bool,
    backfill: bool = False,
) -> DiffResult:
    """Compare freshly fetched products against stored state.

    `full_sweep` must be True only when `products` contains *every* in-stock
    item. Sold-out detection works by absence, so running it against a partial
    page would mark the entire rest of the catalogue as sold out.

    `backfill` marks every emitted event as a cold-start artefact rather than a
    real arrival — see `DetectedEvent.backfill`.
    """
    result = DiffResult(seen_product_ids={p.product_id for p in products}, backfill=backfill)
    existing = store.get_products(result.seen_product_ids)

    for product in products:
        previous = existing.get(product.product_id)

        if previous is None:
            if product.in_stock:
                result.events.append(
                    DetectedEvent(
                        product.product_id,
                        EventType.NEW_IN_STOCK,
                        {"price_paise": product.price_paise, "name": product.name},
                    )
                )
            continue

        was_in_stock = bool(previous["in_stock"])

        if product.in_stock and not was_in_stock:
            result.events.append(
                DetectedEvent(
                    product.product_id,
                    EventType.BACK_IN_STOCK,
                    {"price_paise": product.price_paise},
                )
            )
        elif product.in_stock and was_in_stock:
            old_price = previous["price_paise"]
            if old_price is not None and product.price_paise != old_price:
                result.events.append(
                    DetectedEvent(
                        product.product_id,
                        EventType.PRICE_CHANGE,
                        {"old_paise": old_price, "new_paise": product.price_paise},
                    )
                )

        if changed := _metadata_delta(previous, product):
            result.events.append(
                DetectedEvent(product.product_id, EventType.METADATA_CHANGE, changed)
            )

    if full_sweep:
        vanished = store.known_in_stock_ids() - result.seen_product_ids
        for product_id in sorted(vanished):
            result.events.append(DetectedEvent(product_id, EventType.SOLD_OUT))

    if backfill:
        for event in result.events:
            event.backfill = True
            event.details["backfill"] = True

    return result


def _metadata_delta(previous: Row, product: Product) -> dict[str, Any]:
    """Non-price, non-stock field changes. Recorded for the audit trail only."""
    delta: dict[str, Any] = {}
    for name in METADATA_FIELDS:
        before = previous[name]
        after = getattr(product, name)
        if before != after and not (before is None and after is None):
            delta[name] = {"old": before, "new": after}

    before_categories = set(json.loads(previous["categories_json"] or "[]"))
    if before_categories != set(product.categories):
        delta["categories"] = {
            "old": sorted(before_categories),
            "new": sorted(product.categories),
        }
    return delta


def apply(products: list[Product], diff: DiffResult, store: Store) -> None:
    """Persist products and events.

    Ordering matters: products are written first so the events' foreign keys
    resolve, and sold-out products are flipped here rather than during
    classification so the diff stays a pure function of the inputs.
    """
    for product in products:
        store.upsert_product(product)

    sold_out = [e.product_id for e in diff.events if e.event_type is EventType.SOLD_OUT]
    store.mark_out_of_stock(sold_out)

    for event in diff.events:
        store.record_event(
            event.product_id,
            event.event_type,
            event.details,
            requires_enrichment=event.requires_enrichment,
            requires_inference=event.requires_inference,
        )

    if diff.events:
        log.info(
            "diff: %s | enrichment=%d inference=%d",
            diff.counts(),
            diff.enrichment_count,
            diff.inference_count,
        )
