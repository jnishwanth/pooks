"""Ingest orchestration: the cheap poll and the full sweep."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pooks.config import Config
from pooks.db.store import Store, transaction
from pooks.ingest.diff import DiffResult, apply, classify
from pooks.ingest.store_api import StoreAPIClient
from pooks.models import utcnow

log = logging.getLogger(__name__)


@dataclass
class IngestOutcome:
    kind: str
    diff: DiffResult | None = None
    event_ids: list[int] | None = None
    not_modified: bool = False
    reasons: list[str] | None = None
    products_seen: int = 0

    @property
    def had_changes(self) -> bool:
        return bool(self.diff and self.diff.events)


def build_client(config: Config) -> StoreAPIClient:
    source = config.source
    return StoreAPIClient(
        base_url=source["base_url"],
        user_agent=source["user_agent"],
        min_request_interval_s=source.get("min_request_interval_s", 2.0),
        timeout_s=source.get("request_timeout_s", 30.0),
        max_retries=source.get("max_retries", 3),
    )


async def run_poll(store: Store, client: StoreAPIClient) -> IngestOutcome:
    """Cheap conditional-GET check of the newest in-stock listings.

    Only ever classifies with `full_sweep=False`: this sees the top of the list,
    so absence here means nothing about stock.
    """
    state = store.poll_state()
    result = await client.poll(
        last_modified=state["last_modified"],
        known_total=state["last_instock_total"],
        known_max_id=state["last_max_product_id"],
    )

    if result.not_modified:
        with transaction(store.conn):
            store.update_poll_state(last_poll_at=utcnow(), last_304_at=utcnow())
        log.debug("poll: 304 not modified")
        return IngestOutcome(kind="poll", not_modified=True)

    if not result.changed:
        with transaction(store.conn):
            store.update_poll_state(last_poll_at=utcnow())
        return IngestOutcome(kind="poll", reasons=[])

    diff = classify(result.products, store, full_sweep=False)
    with transaction(store.conn):
        event_ids = apply(result.products, diff, store)
        store.update_poll_state(
            last_modified=result.last_modified,
            last_instock_total=result.instock_total,
            last_max_product_id=result.max_product_id,
            last_poll_at=utcnow(),
        )

    if diff.events:
        log.info("poll: %s -> %s", ", ".join(result.reasons), diff.counts())
    else:
        # Signals moved but no product-level delta. Normal after a restart, or
        # when a change happened outside the newest-listings window the poll
        # sees; the hourly sweep is what catches those.
        log.debug("poll: signals moved (%s) but no product deltas", ", ".join(result.reasons))
    return IngestOutcome(
        kind="poll",
        diff=diff,
        event_ids=event_ids,
        reasons=result.reasons,
        products_seen=len(result.products),
    )


async def run_sweep(store: Store, client: StoreAPIClient) -> IngestOutcome:
    """Full in-stock sweep. The only place sold-out detection is valid.

    Also the safety net for the poll: `orderby=date` sorts by creation, so
    restocks and price changes never surface at the top of the list, and if the
    Last-Modified header ever fails to advance the sweep still catches
    everything within one interval.
    """
    state = store.poll_state()
    is_cold_start = store.is_empty()
    products = await client.fetch_in_stock()

    if not products:
        # An empty sweep would mark the entire catalogue sold out. Far more
        # likely a transport or upstream fault, so refuse to act on it.
        log.error("sweep returned zero in-stock products; refusing to apply")
        return IngestOutcome(kind="sweep", products_seen=0)

    if is_cold_start:
        log.info(
            "cold start: seeding %d in-stock products as backfill. These are "
            "existing shelf stock, not arrivals — enrichment will warm the cache "
            "but inference and notifications are suppressed.",
            len(products),
        )

    diff = classify(products, store, full_sweep=True, backfill=is_cold_start)

    # If the last poll said "not modified" but the sweep found real changes, the
    # Last-Modified header is not a trustworthy sole signal. Surface that.
    last_304 = state["last_304_at"]
    if last_304 and diff.events and last_304 > (state["last_sweep_at"] or ""):
        log.warning(
            "stale-304: poll reported not-modified at %s but sweep found %s. "
            "Last-Modified is unreliable on its own; the total/max-id fallback "
            "and this sweep are carrying detection.",
            state["last_304_at"],
            diff.counts(),
        )

    with transaction(store.conn):
        event_ids = apply(products, diff, store)
        store.update_poll_state(
            last_instock_total=len(products),
            last_max_product_id=max(p.product_id for p in products),
            last_sweep_at=utcnow(),
        )

    log.info("sweep: %d in stock -> %s", len(products), diff.counts() or "no changes")
    return IngestOutcome(
        kind="sweep", diff=diff, event_ids=event_ids, products_seen=len(products)
    )


async def backfill_dates(store: Store, client: StoreAPIClient, limit: int = 200) -> int:
    """Populate creation timestamps, which the Store API omits.

    Not load-bearing for detection (product ids are monotonic), but useful for
    display and for reasoning about arrival rate.
    """
    rows = store.conn.execute(
        "SELECT product_id FROM products WHERE date_created IS NULL LIMIT ?", (limit,)
    ).fetchall()
    ids = [row["product_id"] for row in rows]
    if not ids:
        return 0

    dates = await client.fetch_dates(ids)
    with transaction(store.conn):
        for product_id, values in dates.items():
            store.conn.execute(
                "UPDATE products SET date_created = ?, date_modified = ? WHERE product_id = ?",
                (values["date_created"], values["date_modified"], product_id),
            )
    return len(dates)
