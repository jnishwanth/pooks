"""Cold-start behaviour.

The first sweep into an empty database sees the entire in-stock catalogue as
"new". Treating that literally would infer on every book and notify for all of
them, when in reality they are existing shelf stock.
"""

from __future__ import annotations

from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
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
