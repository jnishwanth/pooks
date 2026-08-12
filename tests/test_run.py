"""The orchestration core: rebuilding a book from cache, and deciding to push.

`pooks blurbs`, `pooks notify` and `rescore` all work entirely from stored rows
rather than re-fetching, so `load_cached` is the single place that knows how a
product, its enrichment and its cached inference fit back together. A book that
is only half present in the cache must be skipped rather than half rebuilt: the
scorer treats a missing rating as a real absence, so a book reassembled without
its enrichment would score as though every source had answered "nothing".

`process_pending` then decides what earns a notification. The score gate is the
obvious half; the tests below cover the other one, which is that only genuine
stock movement counts as an arrival at all.
"""

from __future__ import annotations

import json

from pooks.config import load_config
from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
from pooks.llm.roles import Role
from pooks.models import Product
from pooks.rank.calibrate import calibrate
from pooks.run import load_cached, process_pending, ranked_cached


def _stock(store: Store, products: list[Product]) -> None:
    apply(products, classify(products, store, full_sweep=True), store)


def _enrich(store: Store, product: Product) -> None:
    store.put_enrichment(
        product.book_key,
        {"rating": 4.2, "rating_source": "goodreads", "provenance_json": "{}",
         "refresh_attempts": 0},
    )


def _enrich_pushably(store: Store, product: Product) -> None:
    """Cache enrichment good enough that the book clears the push threshold.

    Also what keeps `process_pending` offline in these tests: an unexpired
    enrichment row is a cache hit, so no source is ever contacted.
    """
    store.put_enrichment(
        product.book_key,
        {
            "rating": 4.6,
            "ratings_count": 200_000,
            "rating_source": "goodreads",
            "synopsis": "A synopsis, which the blurb would be grounded in.",
            "in_price_paise": 200_000,
            "in_price_source": "amazon",
            "in_available": 1,
            "provenance_json": "{}",
            "refresh_attempts": 0,
        },
    )


def _cache_insights(store: Store, product: Product) -> None:
    """Both LLM roles cached, so inference never reaches the network.

    Deliberately an abstained renown: the score is then identical whether or not
    a provider key happens to be configured in the developer's environment,
    since an unavailable client also yields an abstention.
    """
    version = load_config().prompt_version
    store.put_llm(product.book_key, Role.BLURB, version, {"blurb": "a note"})
    store.put_llm(
        product.book_key, Role.RENOWN, version, {"tier": "unknown", "abstained": True}
    )


def _score(store: Store, product: Product, score: float) -> None:
    store.put_score(product.product_id, {"score": score, "confidence": 0.8})


def test_load_cached_skips_a_book_with_no_enrichment(
    store: Store, products: list[Product]
) -> None:
    config = load_config()
    _stock(store, products)
    _enrich(store, products[0])

    rows = {row["product_id"]: row for row in store.ranked_in_stock()}

    assert load_cached(store, config, rows[products[0].product_id]) is not None
    assert load_cached(store, config, rows[products[1].product_id]) is None


def test_load_cached_needs_both_roles_before_it_trusts_the_llm_cache(
    store: Store, products: list[Product]
) -> None:
    """A blurb without a renown judgement is not half an answer — renown feeds
    the score, so filling it with a cached-looking default would rank the book
    as though the model had abstained rather than never been asked."""
    config = load_config()
    product = products[0]
    _stock(store, products)
    _enrich(store, product)
    store.put_llm(product.book_key, Role.BLURB, config.prompt_version, {"blurb": "a note"})

    row = store.get_product(product.product_id)
    assert load_cached(store, config, row)[2].blurb is None

    store.put_llm(
        product.book_key,
        Role.RENOWN,
        config.prompt_version,
        {"tier": "major", "score": 0.7, "abstained": False},
    )
    assert load_cached(store, config, row)[2].blurb == "a note"


def test_ranked_cached_yields_scored_enriched_books_best_first(
    store: Store, products: list[Product]
) -> None:
    """The set both `blurbs` and `notify` want: unscored books have not been
    through the pipeline, and un-enriched ones cannot be rebuilt at all."""
    config = load_config()
    _stock(store, products)
    for product in products[:3]:
        _enrich(store, product)
    _score(store, products[0], 0.4)
    _score(store, products[1], 0.9)
    _score(store, products[3], 0.8)  # scored but never enriched

    found = list(ranked_cached(store, config))

    assert [product.product_id for _, product, _, _ in found] == [
        products[1].product_id,
        products[0].product_id,
    ]
    assert [row["score"] for row, *_ in found] == [0.9, 0.4]


def test_ranked_cached_limit_applies_before_the_skips(
    store: Store, products: list[Product]
) -> None:
    """`limit` bounds the ranking read, not the yield — `pooks blurbs --top 25`
    means "the top 25 books", so a second run is a no-op rather than quietly
    walking deeper into the ranking to make up the number."""
    config = load_config()
    _stock(store, products)
    for product in products[:2]:
        _enrich(store, product)
    _score(store, products[0], 0.9)
    _score(store, products[1], 0.1)

    assert len(list(ranked_cached(store, config, limit=1))) == 1
    assert len(list(ranked_cached(store, config, limit=2))) == 2


async def test_a_genuine_arrival_is_pushed(store: Store, products: list[Product]) -> None:
    config = load_config()
    arrival = products[0]
    _enrich_pushably(store, arrival)
    _cache_insights(store, arrival)
    _stock(store, [arrival])

    result = await process_pending(store, config)

    assert [b.product.product_id for b in result.to_notify] == [arrival.product_id]


async def test_calibrate_predicts_exactly_what_the_pipeline_pushed(
    store: Store, products: list[Product]
) -> None:
    """`pooks calibrate` answers "what would these settings push?", so its answer
    has to be the set `process_pending` actually pushes — one reads stored scores
    back, the other decides live. They share `rank.score.pushable`; this pins
    that the shared gate is fed the same numbers on both sides."""
    config = load_config()
    pushable_books = products[:3]
    for product in pushable_books:
        _enrich_pushably(store, product)
        _cache_insights(store, product)
    # Thin evidence, so it is scored and stored but gated out on confidence.
    _enrich(store, products[3])
    _cache_insights(store, products[3])
    _stock(store, products[:4])

    result = await process_pending(store, config)

    calibration = calibrate(store)
    # The thin-evidence book is in the distribution, so both sides are agreeing
    # about a real decision rather than about "everything that was scored".
    assert calibration.scored == 4
    predicted = calibration.would_push(
        config.push_score_threshold, config.push_min_confidence
    )
    assert {book.name for book in predicted} == {p.name for p in pushable_books}
    assert {book.name for book in predicted} == {b.product.name for b in result.to_notify}


async def test_cold_start_stock_is_scored_but_never_pushed(
    store: Store, products: list[Product]
) -> None:
    """The first sweep sees the whole shelf as "new". Suppression has to survive
    scoring: these books rank exactly as well as a real arrival, so the score
    gate cannot be what holds them back."""
    config = load_config()
    arrival = products[0]
    _enrich_pushably(store, arrival)
    _cache_insights(store, arrival)
    apply([arrival], classify([arrival], store, full_sweep=True, backfill=True), store)

    result = await process_pending(store, config)

    assert result.processed[0].breakdown.score >= config.push_score_threshold
    assert result.processed[0].breakdown.confidence >= config.push_min_confidence
    assert result.to_notify == []


async def test_a_price_change_is_scored_but_never_pushed(
    store: Store, products: list[Product], raw_products, mutate
) -> None:
    """Only stock movement is an arrival. A price cut on a book already on the
    shelf is worth re-scoring — it is the value component — but pushing it would
    re-notify a book the reader has already seen."""
    config = load_config()
    arrival = products[0]
    _enrich_pushably(store, arrival)
    _cache_insights(store, arrival)
    _stock(store, [arrival])
    await process_pending(store, config)

    cheaper = [
        Product.from_store_api(p)
        for p in mutate(raw_products[:1], price_paise={arrival.product_id: 19_900})
    ]
    _stock(store, cheaper)

    result = await process_pending(store, config)

    assert [b.event_type for b in result.processed] == ["PRICE_CHANGE"]
    assert result.processed[0].breakdown.score >= config.push_score_threshold
    assert result.to_notify == []


# --- the repair pass ----------------------------------------------------------


def _with_hardcover_key():
    """A config that can ask for tags, independent of the developer's env."""
    from dataclasses import replace

    config = load_config()
    return replace(config, secrets=replace(config.secrets, hardcover_api_key="test-key"))


def _book_missing_only_its_tags(store: Store) -> Product:
    """Enriched entirely from primary sources, with tags never fetched.

    The state the repair pass's tags predicate exists to catch, and the one
    where a full re-enrich is provably wasted: both tiers are already 0, so
    `merge` keeps the stored rating and price whatever a refetch returns.
    """
    product = Product(
        product_id=99,
        name="Memoirs of a Dutiful Daughter",
        slug="memoirs",
        permalink="https://oldbookdepot.in/product/memoirs",
        isbn="9780140020304",
        price_paise=30_000,
        in_stock=True,
    )
    store.upsert_product(product)
    store.put_enrichment(
        product.book_key,
        {
            "isbn": product.isbn,
            "rating": 4.06,
            "ratings_count": 63,
            "rating_source": "goodreads",
            "provenance_json": "{}",
            "in_price_paise": 33_630,
            "in_price_source": "amazon.in",
            "in_available": 1,
            "in_price_unknown": 0,
            "tags_json": None,
            "refresh_attempts": 0,
        },
    )
    return product


async def test_a_tags_only_repair_asks_hardcover_and_nothing_else(
    store: Store, monkeypatch
) -> None:
    """The whole point of the cheap path: obtaining a tag list must not cost
    Goodreads' 60s and Amazon's 90s for answers `merge` would discard."""
    from pooks.enrich import hardcover
    from pooks.enrich.pipeline import Enricher
    from pooks.run import refresh_improvable

    product = _book_missing_only_its_tags(store)
    asked: list[str] = []

    async def _tags(client, isbn, api_key):
        asked.append(isbn)
        return {"genre": ["memoir"], "mood": ["reflective"]}

    async def _refuse(*args, **kwargs):
        raise AssertionError("a tags-only gap must not re-run the enrichment chain")

    monkeypatch.setattr(hardcover, "fetch_tags", _tags)
    monkeypatch.setattr(Enricher, "_fetch_fresh", _refuse)

    result = await refresh_improvable(store, _with_hardcover_key())

    assert asked == [product.isbn]
    assert (result.attempted, result.improved, result.unchanged) == (1, 1, 0)

    row = store.get_enrichment(product.book_key)
    assert json.loads(row["tags_json"]) == {"genre": ["memoir"], "mood": ["reflective"]}
    assert (row["rating_source"], row["in_price_source"]) == ("goodreads", "amazon.in")
    assert row["refresh_attempts"] == 1


async def test_filling_tags_counts_as_improved_without_rebuilding_the_ranking(
    store: Store, monkeypatch
) -> None:
    """Both halves of the reporting. Rating and price are unchanged by a tag
    fill, so measuring improvement on those alone reported the entire backfill
    as "0 improved" — and rescoring on it would rebuild every score for a value
    `rank.score` does not read."""
    from pooks.enrich import hardcover
    from pooks.enrich.pipeline import Enricher
    from pooks.run import refresh_improvable

    _book_missing_only_its_tags(store)

    async def _tags(client, isbn, api_key):
        return {"genre": ["memoir"]}

    async def _refuse(*args, **kwargs):
        raise AssertionError("a tags-only gap must not re-run the enrichment chain")

    monkeypatch.setattr(hardcover, "fetch_tags", _tags)
    monkeypatch.setattr(Enricher, "_fetch_fresh", _refuse)

    result = await refresh_improvable(store, _with_hardcover_key())

    assert result.improved == 1
    scored = store.conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"]
    assert scored == 0, "a tag list does not change the ranking"


async def test_a_tags_repair_with_no_answer_stays_retriable(
    store: Store, monkeypatch
) -> None:
    """None is "never answered", so the column has to stay NULL — writing `{}`
    would settle the book on a lookup that never happened, and it would never be
    offered up again."""
    from pooks.enrich import hardcover
    from pooks.enrich.pipeline import Enricher
    from pooks.run import refresh_improvable

    product = _book_missing_only_its_tags(store)

    async def _no_answer(client, isbn, api_key):
        return None

    async def _refuse(*args, **kwargs):
        raise AssertionError("a tags-only gap must not re-run the enrichment chain")

    monkeypatch.setattr(hardcover, "fetch_tags", _no_answer)
    monkeypatch.setattr(Enricher, "_fetch_fresh", _refuse)

    result = await refresh_improvable(store, _with_hardcover_key())

    assert (result.attempted, result.improved, result.unchanged) == (1, 0, 1)
    assert store.get_enrichment(product.book_key)["tags_json"] is None


async def test_a_book_below_the_refresh_floor_is_tagged_but_not_re_enriched(
    store: Store, monkeypatch
) -> None:
    """The floor rations the expensive repair, not the cheap one. A book that
    cannot clear the push threshold still has to be tagged — tags are a
    browsing filter, and one Hardcover call is paced at a second — but the
    60s/90s chain its fallback rating would otherwise earn must not run on it.
    """
    from pooks.enrich import hardcover
    from pooks.enrich.pipeline import Enricher
    from pooks.run import refresh_improvable

    config = _with_hardcover_key()
    product = _book_missing_only_its_tags(store)
    store.conn.execute(
        "UPDATE enrichment SET rating_source = 'open_library' WHERE book_key = ?",
        (product.book_key,),
    )
    _score(store, product, config.refresh_min_score - 0.1)

    asked: list[str] = []

    async def _tags(client, isbn, api_key):
        asked.append(isbn)
        return {"genre": ["memoir"]}

    async def _refuse(*args, **kwargs):
        raise AssertionError("below the floor, the enrichment chain must not run")

    monkeypatch.setattr(hardcover, "fetch_tags", _tags)
    monkeypatch.setattr(Enricher, "_fetch_fresh", _refuse)

    result = await refresh_improvable(store, config)

    assert asked == [product.isbn]
    assert result.improved == 1
    row = store.get_enrichment(product.book_key)
    assert json.loads(row["tags_json"]) == {"genre": ["memoir"]}
    assert row["rating_source"] == "open_library", "the chain was never re-run"


async def test_a_book_over_the_refresh_floor_still_gets_the_full_chain(
    store: Store, monkeypatch
) -> None:
    """The guard above must not swallow the repair it was added beside: a
    fallback rating on a book that can be pushed is exactly what the expensive
    path exists for."""
    from pooks.enrich.pipeline import Enricher
    from pooks.enrich.sources import BookFacts, IndianPrice
    from pooks.run import refresh_improvable

    config = _with_hardcover_key()
    product = _book_missing_only_its_tags(store)
    store.conn.execute(
        "UPDATE enrichment SET rating_source = 'open_library' WHERE book_key = ?",
        (product.book_key,),
    )
    _score(store, product, config.refresh_min_score + 0.1)

    async def _fresh(self, client, prod, book_key):
        return BookFacts(
            book_key=book_key,
            rating=4.06,
            ratings_count=63,
            rating_source="goodreads",
            indian_price=IndianPrice(
                price_paise=33_630, source="amazon.in", available_in_india=True
            ),
        )

    monkeypatch.setattr(Enricher, "_fetch_fresh", _fresh)

    result = await refresh_improvable(store, config)

    assert result.improved == 1
    assert store.get_enrichment(product.book_key)["rating_source"] == "goodreads"
