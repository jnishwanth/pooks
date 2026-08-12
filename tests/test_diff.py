"""Event classification, and the cost policy it enforces."""

from __future__ import annotations

from typing import Any

from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
from pooks.models import EventType, Product, utcnow


def _seed(store: Store, products: list[Product]) -> None:
    diff = classify(products, store, full_sweep=True)
    apply(products, diff, store)
    store.mark_events_processed([row["id"] for row in store.unprocessed_events()])


def _parse(raw: list[dict[str, Any]]) -> list[Product]:
    return [Product.from_store_api(p) for p in raw]


def test_first_sight_is_new_in_stock(store: Store, products: list[Product]) -> None:
    diff = classify(products, store, full_sweep=True)

    assert diff.counts() == {str(EventType.NEW_IN_STOCK): len(products)}
    assert all(e.requires_enrichment and e.requires_inference for e in diff.events)


def test_unchanged_sweep_produces_no_events(store: Store, products: list[Product]) -> None:
    _seed(store, products)

    diff = classify(products, store, full_sweep=True)

    assert diff.events == []


# --- the headline requirement: a sold-out delta must cost nothing -------------


def test_sold_out_requires_no_enrichment_and_no_inference(
    store: Store, products: list[Product], raw_products: list[dict[str, Any]], mutate
) -> None:
    _seed(store, products)
    remaining = _parse(mutate(raw_products, drop_ids={233188, 233110}))

    diff = classify(remaining, store, full_sweep=True)

    assert diff.counts() == {str(EventType.SOLD_OUT): 2}
    assert diff.enrichment_count == 0
    assert diff.inference_count == 0


def test_sold_out_updates_stock_flag_silently(
    store: Store, products: list[Product], raw_products: list[dict[str, Any]], mutate
) -> None:
    _seed(store, products)
    remaining_raw = mutate(raw_products, drop_ids={233188})
    remaining = _parse(remaining_raw)

    diff = classify(remaining, store, full_sweep=True)
    apply(remaining, diff, store)

    assert store.get_product(233188)["in_stock"] == 0
    events = store.events_for_product(233188)
    assert events[0]["event_type"] == str(EventType.SOLD_OUT)
    assert events[0]["requires_enrichment"] == 0
    assert events[0]["requires_inference"] == 0


def test_partial_poll_never_emits_sold_out(
    store: Store, products: list[Product], raw_products: list[dict[str, Any]], mutate
) -> None:
    """Sold-out detection works by absence. Running it on a partial page would
    mark the rest of the catalogue as sold out."""
    _seed(store, products)
    partial = _parse(mutate(raw_products, drop_ids={233188, 233180, 233165, 233118}))

    diff = classify(partial, store, full_sweep=False)

    assert diff.events == []


# --- the events that do cost something ---------------------------------------


def test_price_change_enriches_but_does_not_infer(
    store: Store, products: list[Product], raw_products: list[dict[str, Any]], mutate
) -> None:
    _seed(store, products)
    cheaper = _parse(mutate(raw_products, price_paise={233188: 24900}))

    diff = classify(cheaper, store, full_sweep=True)

    assert diff.counts() == {str(EventType.PRICE_CHANGE): 1}
    event = diff.events[0]
    assert event.details == {"old_paise": 39900, "new_paise": 24900}
    assert event.requires_enrichment is True
    assert event.requires_inference is False


def test_restock_enriches_from_cache_without_inference(
    store: Store, products: list[Product], raw_products: list[dict[str, Any]], mutate
) -> None:
    """A relisted book already has enrichment cached by book_key, so it must not
    pay for inference a second time."""
    _seed(store, products)
    without = _parse(mutate(raw_products, drop_ids={233110}))
    apply(without, classify(without, store, full_sweep=True), store)

    restored = _parse(raw_products)
    diff = classify(restored, store, full_sweep=True)

    assert diff.counts() == {str(EventType.BACK_IN_STOCK): 1}
    assert diff.events[0].requires_enrichment is True
    assert diff.events[0].requires_inference is False


def test_metadata_change_costs_nothing(
    store: Store, products: list[Product], raw_products: list[dict[str, Any]], mutate
) -> None:
    _seed(store, products)
    renamed = _parse(mutate(raw_products, rename={233118: "Dirty Tricks by M. Dibdin"}))

    diff = classify(renamed, store, full_sweep=True)

    assert diff.counts() == {str(EventType.METADATA_CHANGE): 1}
    assert diff.enrichment_count == 0
    assert diff.inference_count == 0


def test_new_arrival_alongside_sold_out(
    store: Store, products: list[Product], raw_products: list[dict[str, Any]], mutate
) -> None:
    """A realistic bulk-upload tick: some books land, others vanish."""
    _seed(store, products)

    arrival = dict(raw_products[0])
    arrival["id"] = 999001
    arrival["name"] = "A Brand New Arrival by Someone"
    next_tick = _parse(mutate(raw_products, drop_ids={233107}) + [arrival])

    diff = classify(next_tick, store, full_sweep=True)

    assert diff.counts() == {
        str(EventType.NEW_IN_STOCK): 1,
        str(EventType.SOLD_OUT): 1,
    }
    assert diff.inference_count == 1


def test_pending_event_count_tracks_the_queue(store: Store, products: list[Product]) -> None:
    """The daemon keys backlog draining on this. It previously processed only
    when a poll found changes, so a cold-start queue of ~630 events stalled the
    moment the next poll returned 304."""
    assert store.pending_event_count() == 0

    diff = classify(products, store, full_sweep=True, backfill=True)
    apply(products, diff, store)
    assert store.pending_event_count() == len(products)

    rows = store.unprocessed_events(limit=2)
    store.mark_events_processed([r["id"] for r in rows])
    assert store.pending_event_count() == len(products) - 2


class _FakeClient:
    """Returns a fixed product list and Last-Modified header."""

    def __init__(self, products: list[Product], header: str | None) -> None:
        self._products, self._header = products, header

    async def fetch_in_stock(self, page_size: int = 100):
        return self._products, self._header


def _seed_processed(store: Store, products: list[Product]) -> None:
    apply(products, classify(products, store, full_sweep=True), store)
    store.mark_events_processed([r["id"] for r in store.unprocessed_events()])


async def test_stale_header_is_reported(store: Store, products: list[Product], caplog) -> None:
    """A header that did not move despite a real sold-out is genuinely
    suspicious, and worth saying so."""
    import logging

    from pooks.ingest.pipeline import run_sweep

    header = "Mon, 10 Aug 2026 19:25:09 GMT"
    _seed_processed(store, products)
    store.update_poll_state(last_modified=header)

    with caplog.at_level(logging.WARNING):
        await run_sweep(store, _FakeClient(products[:-1], header))

    assert "did not advance" in caplog.text


async def test_advanced_header_is_not_reported(
    store: Store, products: list[Product], caplog
) -> None:
    """The regression. An earlier version warned whenever a sweep found changes
    after any 304 — which happens routinely, because the change simply occurred
    after the last poll — and its message asserted the header was unreliable
    when nothing supported that.

    Checked against the live site: with zero products created in the window,
    Last-Modified still advanced (10 Aug 19:25 -> 11 Aug 17:37) while the
    in-stock total fell 634 -> 633. It does track pure stock changes.
    """
    import logging

    from pooks.ingest.pipeline import run_sweep

    _seed_processed(store, products)
    store.update_poll_state(last_modified="Mon, 10 Aug 2026 19:25:09 GMT", last_304_at=utcnow())

    with caplog.at_level(logging.WARNING):
        await run_sweep(store, _FakeClient(products[:-1], "Tue, 11 Aug 2026 17:37:19 GMT"))

    assert "did not advance" not in caplog.text
