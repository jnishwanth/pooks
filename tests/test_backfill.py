"""Cold-start behaviour.

The first sweep into an empty database sees the entire in-stock catalogue as
"new". Treating that literally would infer on every book and notify for all of
them, when in reality they are existing shelf stock.
"""

from __future__ import annotations

from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
from pooks.ingest.pipeline import backfill_dates
from pooks.models import EventType, Product


def test_cold_start_suppresses_inference_and_notification(
    store: Store, products: list[Product]
) -> None:
    assert store.is_empty()

    diff = classify(products, store, full_sweep=True, backfill=True)

    assert diff.counts() == {str(EventType.NEW_IN_STOCK): len(products)}
    # Enrichment still runs: it warms the ISBN-keyed cache, which is what makes
    # a later relist free.
    assert diff.enrichment_count == len(products)
    # But nothing is inferred or pushed.
    assert diff.inference_count == 0
    assert not any(e.should_notify for e in diff.events)


def test_backfill_flag_is_persisted(store: Store, products: list[Product]) -> None:
    diff = classify(products, store, full_sweep=True, backfill=True)
    apply(products, diff, store)

    rows = store.unprocessed_events()
    assert rows, "events should be recorded even when suppressed"
    assert all(row["requires_inference"] == 0 for row in rows)
    assert all(row["requires_enrichment"] == 1 for row in rows)
    assert all('"backfill": true' in row["details_json"] for row in rows)


def test_arrivals_after_cold_start_are_not_suppressed(
    store: Store, products: list[Product], raw_products, mutate
) -> None:
    """The suppression must apply only to the seeding sweep."""
    seed = classify(products, store, full_sweep=True, backfill=True)
    apply(products, seed, store)
    assert not store.is_empty()

    arrival = dict(raw_products[0])
    arrival["id"] = 999002
    arrival["name"] = "A Genuine New Arrival by Someone"
    later = [*products, Product.from_store_api(arrival)]

    diff = classify(later, store, full_sweep=True, backfill=False)

    assert diff.counts() == {str(EventType.NEW_IN_STOCK): 1}
    assert diff.inference_count == 1
    assert diff.events[0].should_notify is True


# --- creation dates -----------------------------------------------------------
#
# The Store API omits `date_created`, so it comes from wp/v2 separately. The
# daemon runs this on the hourly sweep, bounded per call, because ids wp/v2
# never answers for are re-asked every time it runs.


class _DateClient:
    """Records what it was asked for, so "asked for nothing" is assertable."""

    def __init__(self) -> None:
        self.requested: list[list[int]] = []

    async def fetch_dates(self, product_ids: list[int]) -> dict[int, dict[str, str]]:
        self.requested.append(product_ids)
        return {
            pid: {"date_created": "2026-08-01T09:00:00", "date_modified": "2026-08-02T09:00:00"}
            for pid in product_ids
        }


async def test_backfill_dates_fills_the_arrival_date(store: Store, products: list[Product]) -> None:
    apply(products, classify(products, store, full_sweep=True), store)
    assert all(row["date_created"] is None for row in store.in_stock_products())

    filled = await backfill_dates(store, _DateClient())

    assert filled == len(products)
    assert all(row["date_created"] == "2026-08-01T09:00:00" for row in store.in_stock_products())


async def test_backfill_dates_asks_for_nothing_once_filled(
    store: Store, products: list[Product]
) -> None:
    """What bounds the cost for rows it can fill: once a book has its dates it
    drops out of the SELECT, and a fully dated catalogue issues no request."""
    apply(products, classify(products, store, full_sweep=True), store)
    await backfill_dates(store, _DateClient())

    client = _DateClient()
    assert await backfill_dates(store, client) == 0
    assert client.requested == []


async def test_a_sweep_does_not_blank_a_backfilled_date(
    store: Store, products: list[Product]
) -> None:
    """`Product.from_store_api` never sets either date, so every sweep carries
    None — the upsert COALESCEs for exactly this reason, and without it the
    backfill would undo itself on the very sweep that runs alongside it."""
    apply(products, classify(products, store, full_sweep=True), store)
    await backfill_dates(store, _DateClient())

    apply(products, classify(products, store, full_sweep=True), store)

    assert all(row["date_created"] == "2026-08-01T09:00:00" for row in store.in_stock_products())
