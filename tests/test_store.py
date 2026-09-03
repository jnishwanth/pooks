"""The queries the CLI and the dashboard read the pipeline's state through.

These used to be hand-written SQL at each call site, which is how the same
count came to exist in three places. Covering them here is what makes a schema
change checkable in one file rather than by grepping for table names.
"""

from __future__ import annotations

import re
import sqlite3

from pooks.db.store import SCHEMA_PATH, Store, _migrate, connect, product_from_row
from pooks.ingest.diff import apply, classify
from pooks.llm.roles import Role
from pooks.models import Product

BREAKDOWN = {"score": 0.7, "confidence": 0.8}


def _stock(store: Store, products: list[Product]) -> None:
    apply(products, classify(products, store, full_sweep=True), store)


def _enrich(store: Store, book_key: str) -> None:
    store.put_enrichment(book_key, {"provenance_json": "{}", "refresh_attempts": 0})


def test_every_product_field_survives_the_round_trip(store: Store) -> None:
    """The products table is the only copy of a listing the pipeline keeps.

    A field written but not read back is invisible: enrichment, scoring and the
    digest all work from `product_from_row`, so a dropped field degrades every
    stored read while a freshly fetched product looks correct. This asserts on
    the whole model rather than a chosen few fields, so a field added to
    `Product` is covered without editing the test.
    """
    original = Product(
        product_id=101,
        name="Memoirs of a Dutiful Daughter by Simone de Beauvoir (Penguin)",
        slug="memoirs-of-a-dutiful-daughter",
        permalink="https://oldbookdepot.in/product/memoirs",
        isbn="9780140020304",
        author="Simone de Beauvoir",
        publisher="Penguin",
        book_format="Paperback",
        pages=360,
        condition="Good",
        categories=["Non Fiction", "Biography"],
        price_paise=25_000,
        regular_price_paise=40_000,
        in_stock=True,
        date_created="2024-01-01T00:00:00",
        date_modified="2024-02-02T00:00:00",
        image_url="https://oldbookdepot.in/wp-content/uploads/2026/08/memoirs.jpg",
    )
    store.upsert_product(original)

    assert product_from_row(store.get_product(101)) == original


def test_the_description_survives_the_round_trip(store: Store, products: list[Product]) -> None:
    """`upsert_product` and `product_from_row` are both generated from
    `Product.model_fields`, so a field with no column behind it is a loud failure
    here rather than a value silently dropped on every read."""
    described = next(p for p in products if p.description)
    store.upsert_product(described)

    assert product_from_row(store.get_product(described.product_id)) == described


def test_a_cleared_description_is_written_through(store: Store) -> None:
    """Unlike the two date columns, this one is assigned rather than COALESCEd.

    The dates are preserved because the Store API never carries them — only
    wp/v2 knows them, so a sweep's None would blank what the date backfill
    learned. The description arrives in the very same payload, so a shop that
    removes one is reporting a fact and must not be overridden by a stale copy.
    """
    product = Product(product_id=7, name="A Book", description="Some copy", in_stock=True)
    store.upsert_product(product)
    assert store.get_product(7)["description"] == "Some copy"

    store.upsert_product(product.model_copy(update={"description": None}))

    assert store.get_product(7)["description"] is None


def test_opening_a_legacy_database_adds_the_description_column(tmp_path) -> None:
    """`CREATE TABLE IF NOT EXISTS` will not add a column to a table that already
    exists, which is what `_MIGRATIONS` is for. `data/pooks.db` predates this
    column, so the shape without it is the one actually deployed."""
    db_path = tmp_path / "pooks.db"
    store = Store(connect(db_path))
    store.upsert_product(Product(product_id=1, name="An Older Book"))
    store.conn.commit()
    store.conn.close()

    # The deployed shape: everything this schema has, except the new column.
    # Dropping it is exact where a hand-written CREATE TABLE would drift.
    legacy = sqlite3.connect(db_path)
    legacy.execute("ALTER TABLE products DROP COLUMN description")
    legacy.commit()
    legacy.close()

    store = Store(connect(db_path))

    # The pre-existing row is still readable, and the column is there to fill.
    assert store.get_product(1)["description"] is None
    store.upsert_product(Product(product_id=1, name="An Older Book", description="Now described"))
    assert store.get_product(1)["description"] == "Now described"


def test_upsert_keeps_what_the_store_api_cannot_report(store: Store) -> None:
    """`first_seen_at` and the two dates are not the payload's to overwrite.

    The Store API omits creation timestamps entirely, so every sweep carries
    None for them; only `ingest.backfill_dates` ever learns them, from wp/v2.
    Assigning them straight across would blank that out on the next sweep.
    """
    product = Product(product_id=7, name="A Book", in_stock=True)
    store.upsert_product(product)
    assert product.date_created is None  # as every sweep sees it

    store.conn.execute(
        "UPDATE products SET date_created = ?, date_modified = ?, first_seen_at = ? "
        "WHERE product_id = 7",
        ("2023-05-05T00:00:00", "2023-06-06T00:00:00", "2023-01-01T00:00:00"),
    )
    store.upsert_product(product.model_copy(update={"price_paise": 12_345}))
    after = dict(store.get_product(7))

    assert after["date_created"] == "2023-05-05T00:00:00"
    assert after["date_modified"] == "2023-06-06T00:00:00"
    assert after["first_seen_at"] == "2023-01-01T00:00:00"
    assert after["price_paise"] == 12_345


def test_in_stock_products_narrows_to_the_unenriched(store: Store, products: list[Product]) -> None:
    """`pooks enrich` without --force must skip what is already cached; with it,
    the same query has to return everything so the cache can be re-fetched."""
    _stock(store, products)
    _enrich(store, products[0].book_key)

    every = store.in_stock_products()
    todo = store.in_stock_products(missing_enrichment=True)

    assert len(every) == len(products)
    assert len(todo) == len(every) - 1
    assert products[0].book_key not in {row["book_key"] for row in todo}


def test_in_stock_products_excludes_sold_out(store: Store, products: list[Product]) -> None:
    _stock(store, products)
    store.mark_out_of_stock([products[0].product_id])

    assert len(store.in_stock_products()) == len(products) - 1


def test_row_limits_are_unlimited_by_default(store: Store, products: list[Product]) -> None:
    """Every `limit` on the store defaults to "all rows".

    A cap picked to be comfortably large is a silent truncation with a delay
    fuse: the dashboard's hardcoded 634 hid books from a *search* because it
    filtered downstream of the fetch, and `rescore_in_stock`'s 1000 would have
    left part of the catalogue scored by the previous scoring function.
    """
    _stock(store, products)
    store.mark_out_of_stock([products[0].product_id])
    buyable = len(products) - 1

    for rows in (store.ranked_in_stock(), store.in_stock_products()):
        assert len(rows) == buyable

    assert len(store.ranked_in_stock(limit=2)) == 2
    assert len(store.in_stock_products(2)) == 2


def test_product_counts_separates_tracked_from_buyable(
    store: Store, products: list[Product]
) -> None:
    """Sold-out rows are retained deliberately — they are what makes a price
    drop across relists visible — so 'tracked' and 'in stock' diverge."""
    assert store.product_counts() == {"tracked": 0, "in_stock": 0, "scored": 0}

    _stock(store, products)
    store.mark_out_of_stock([products[0].product_id])
    store.put_score(products[1].product_id, BREAKDOWN)

    assert store.product_counts() == {
        "tracked": len(products),
        "in_stock": len(products) - 1,
        "scored": 1,
    }


def test_event_counts_by_type_is_ordered_by_frequency(
    store: Store, products: list[Product]
) -> None:
    _stock(store, products)
    store.mark_out_of_stock([products[0].product_id])
    _stock(store, products[:1])

    counts = store.event_counts_by_type()

    assert [row["n"] for row in counts] == sorted((row["n"] for row in counts), reverse=True)
    assert dict(counts[0]) == {"event_type": "NEW_IN_STOCK", "n": len(products)}


def test_get_llm_many_returns_one_entry_per_cached_key(store: Store) -> None:
    store.put_llm("isbn:1", Role.BLURB, 1, {"blurb": "one"})
    store.put_llm("isbn:2", Role.BLURB, 1, {"blurb": "two"})
    store.put_llm("isbn:2", Role.RENOWN, 1, {"tier": "known"})
    store.put_llm("isbn:3", Role.BLURB, 2, {"blurb": "newer prompt"})

    found = store.get_llm_many(["isbn:1", "isbn:2", "isbn:3"], Role.BLURB, 1)

    assert found == {"isbn:1": {"blurb": "one"}, "isbn:2": {"blurb": "two"}}
    assert store.get_llm_many([], Role.BLURB, 1) == {}


def test_scores_without_enrichment_behind_them_are_pruned(
    store: Store, products: list[Product]
) -> None:
    """A score outlives the enrichment it was computed from — after a book_key
    change, say — and would otherwise sit in `top` forever, computed under a
    scoring function nothing else in the list shares."""
    _stock(store, products)
    _enrich(store, products[0].book_key)
    store.put_score(products[0].product_id, BREAKDOWN)
    store.put_score(products[1].product_id, BREAKDOWN)

    assert store.prune_unbacked_scores() == 1
    assert store.product_counts()["scored"] == 1


def test_a_database_written_before_the_cover_column_gains_it_on_open(tmp_path) -> None:
    """Every deployed database predates this column, and `product_from_row`
    rebuilds a `Product` from `Product.model_fields` — so without the migration
    the first stored read of any listing raises, not just the ones with a photo.

    The legacy schema is built by stripping the column back out of `schema.sql`
    rather than by pasting an old copy, so this cannot pass by testing a
    snapshot that has drifted from the real one.
    """
    legacy = re.sub(
        r"\n *-- The shop's own photograph.*?\n *image_url +TEXT,",
        "",
        SCHEMA_PATH.read_text(),
        flags=re.S,
    )
    assert "image_url" not in legacy, "the column is no longer stripped by this pattern"

    db_path = tmp_path / "pooks.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(legacy)
    conn.execute(
        "INSERT INTO products (product_id, book_key, name, first_seen_at, last_seen_at)"
        " VALUES (7, 'isbn:1', 'Shelf Stock', 't', 't')"
    )
    conn.commit()
    conn.close()

    reopened = Store(connect(db_path))

    assert product_from_row(reopened.get_product(7)).image_url is None


def test_opening_a_database_rounds_a_legacy_rating(tmp_path) -> None:
    """Rounding to 2dp happens on construction of a `RatingResult`, which was
    added after the first rows were written, and the merge that preceded the
    observation ledger carried a stored rating forward verbatim, so a re-enrich
    could never repair one. Hardcover and Open Library return raw computed averages, so those rows
    rendered `4.06349206349206` on the card.

    Repaired on open rather than by a one-shot script because the databases that
    have it are already deployed.
    """
    db_path = tmp_path / "pooks.db"
    store = Store(connect(db_path))
    store.put_enrichment("isbn:1", {"provenance_json": "{}", "refresh_attempts": 0})
    store.conn.execute("UPDATE enrichment SET rating = 4.063492063492063")
    store.conn.commit()
    store.conn.close()

    reopened = Store(connect(db_path))

    assert reopened.get_enrichment("isbn:1")["rating"] == 4.06


def test_rounding_leaves_an_already_clean_rating_alone(tmp_path) -> None:
    """The migration runs on every open, so it has to converge rather than
    rewrite the table each time — the WHERE clause is load-bearing, not an
    optimisation.

    `total_changes` is what pins that: it counts the rows written since the
    connection opened, and an unguarded `UPDATE enrichment SET rating =
    ROUND(rating, 2)` still leaves 4.13 as 4.13, so the value assertions below
    cannot tell the two apart. It matters because `serve.app._open` calls
    `connect` — and therefore this migration — inside every HTTP request, so
    losing the clause turns each page load into a full-table write against the
    database the daemon is holding.
    """
    db_path = tmp_path / "pooks.db"
    store = Store(connect(db_path))
    store.put_enrichment("isbn:1", {"provenance_json": "{}", "refresh_attempts": 0, "rating": 4.13})
    store.conn.commit()
    store.conn.close()

    reopened = Store(connect(db_path))

    assert reopened.conn.total_changes == 0, "opening a clean database must write nothing"
    assert reopened.get_enrichment("isbn:1")["rating"] == 4.13
    assert (
        reopened.conn.execute(
            "SELECT COUNT(*) n FROM enrichment "
            "WHERE rating IS NOT NULL AND rating != ROUND(rating, 2)"
        ).fetchone()["n"]
        == 0
    )


def test_a_clean_database_is_opened_without_a_write_statement(tmp_path) -> None:
    """The probe in front of each repair, not just the repair's own WHERE.

    `serve.app._open` calls `connect` — and therefore `_migrate` — inside every
    single HTTP request. An unconditional UPDATE rewrites no rows once the data
    is clean, so `total_changes` above stays 0 either way, but SQLite still
    opens a write transaction and takes the WAL writer lock to discover that —
    against the very database the daemon is writing to. Reading first costs a
    shared lock instead.

    Asserted by tracing the statements `_migrate` actually issues, which is a
    runtime observation rather than a read of the source.
    """
    db_path = tmp_path / "pooks.db"
    store = Store(connect(db_path))
    store.put_enrichment("isbn:1", {"provenance_json": "{}", "refresh_attempts": 0, "rating": 4.13})
    store.conn.commit()
    store.conn.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    issued: list[str] = []
    conn.set_trace_callback(issued.append)
    _migrate(conn)

    assert not [s for s in issued if s.lstrip().upper().startswith("UPDATE")], issued
