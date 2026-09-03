"""The weekly summary, which is the only thing that notices a silent failure.

Every degradation this reports on — a blocked rating source, a stalled queue, a
lapsed key — leaves the pipeline running and simply makes the digest worse, so
the numbers here are the whole alarm. A miscounted denominator is therefore not
a cosmetic bug: coverage that measures itself against the wrong catalogue reads
as healthy exactly when the shelf has grown faster than the enrichment has.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pooks.config import load_config
from pooks.db.store import Store
from pooks.enrich.quality import MAX_REFRESH_ATTEMPTS
from pooks.ingest.diff import apply, classify
from pooks.llm.roles import Role
from pooks.models import Product
from pooks.rank.health import Health, _warnings, collect, render


def _stock(store: Store, products: list[Product]) -> None:
    apply(products, classify(products, store, full_sweep=True), store)


def _enrich(store: Store, book_key: str, **columns: object) -> None:
    store.put_enrichment(book_key, {"provenance_json": "{}", "refresh_attempts": 0, **columns})


def test_coverage_is_measured_against_the_in_stock_catalogue(
    store: Store, products: list[Product]
) -> None:
    """Enrichment is keyed by book, not by listing, so it outlives the listing
    that paid for it. Counting a sold-out book's enrichment would make coverage
    climb every time the shop sells something."""
    _stock(store, products)
    _enrich(
        store,
        products[0].book_key,
        rating=4.1,
        rating_source="goodreads",
        in_price_paise=30_000,
    )
    _enrich(store, products[1].book_key, rating=4.4, rating_source="goodreads")
    store.mark_out_of_stock([products[1].product_id])
    buyable = len(products) - 1

    health = collect(store, load_config())

    assert (health.in_stock, health.enriched) == (buyable, 1)
    assert (health.with_rating, health.with_indian_price) == (1, 1)
    assert health.enrichment_coverage == pytest.approx(1 / buyable)
    assert health.rating_coverage == health.price_coverage == health.enrichment_coverage


def test_description_coverage_counts_the_listings_that_carry_one(
    store: Store, products: list[Product]
) -> None:
    """The description arrives with the listing rather than with enrichment, so
    it is the one coverage figure that should be near-total on a cold catalogue.
    Reporting it is how a shop that stops sending them becomes visible before
    the blurbs it grounds quietly get worse."""
    _stock(store, products)
    described = sum(1 for p in products if p.description)

    health = collect(store, load_config())

    assert described and described < len(products), "fixture needs both shapes"
    assert health.with_description == described
    assert health.description_coverage == pytest.approx(described / len(products))
    assert f"description    {described}" in render(health)


def test_the_enrichment_warning_fires_at_the_percentage_it_reports() -> None:
    """The warning threshold and the rendered figure are the same coverage, so
    the summary can never advise running 'pooks backfill' beside a line reading
    100%. They used to be written two ways — a division in `render`, the
    threshold multiplied out in `_warnings`."""
    assert "only 8/10 in-stock books enriched" in " ".join(
        _warnings(Health(in_stock=10, enriched=8, with_rating=10))
    )
    # Exactly the threshold, so the shelf is as covered as it is asked to be.
    assert _warnings(Health(in_stock=10, enriched=9, with_rating=10)) == []

    assert "enriched       9 (90%)" in render(Health(in_stock=10, enriched=9))


def test_a_rating_from_off_the_chain_counts_as_a_fallback(
    store: Store, products: list[Product]
) -> None:
    """A fallback rating is the repair pass's work list, and an empty chain is
    the documented way to turn rating lookup off — with nothing asked for, no
    source can be the wrong one."""
    _stock(store, products)
    _enrich(store, products[0].book_key, rating=4.1, rating_source="goodreads")
    _enrich(store, products[1].book_key, rating=4.3, rating_source="goodreads")
    _enrich(store, products[2].book_key, rating=3.9, rating_source="openlibrary")
    config = load_config()
    assert config.primary_rating_source == "goodreads"

    assert collect(store, config).fallback_rating == 1
    assert collect(store, replace(config, ratings={"chain": []})).fallback_rating == 0


def test_books_past_the_retry_cap_are_counted(store: Store, products: list[Product]) -> None:
    """The cap is what stops a genuinely unfindable book being re-fetched
    forever; the count is the only place its cost surfaces. It has never been
    non-zero on real data, so this is its only exercise."""
    _stock(store, products)
    _enrich(store, products[0].book_key, refresh_attempts=MAX_REFRESH_ATTEMPTS)
    _enrich(store, products[1].book_key, refresh_attempts=MAX_REFRESH_ATTEMPTS - 1)

    health = collect(store, load_config())

    assert health.exhausted == 1
    assert health.improvable == 1  # the one with retries left


def test_with_blurb_counts_only_the_configured_prompt_version(
    store: Store, products: list[Product]
) -> None:
    """Bumping `[llm].prompt_version` invalidates the cache, so the count has to
    fall — a reader pinned to the old version would report full coverage of
    blurbs the digest can no longer use."""
    config = load_config()
    _stock(store, products)
    store.put_llm(products[0].book_key, Role.BLURB, config.prompt_version, {"blurb": "a"})
    store.put_llm(products[1].book_key, Role.BLURB, config.prompt_version + 1, {"blurb": "b"})
    store.put_llm(products[2].book_key, Role.RENOWN, config.prompt_version, {"tier": "known"})

    assert collect(store, config).with_blurb == 1


def test_an_empty_catalogue_reports_zeros_rather_than_failing(store: Store) -> None:
    """Every count is a SUM over no rows, which SQLite returns as NULL, and every
    coverage divides by the in-stock total. A fresh install must render."""
    health = collect(store, load_config())

    assert health == Health()
    assert health.enrichment_coverage == 0.0
    assert "in stock       0" in render(health)
