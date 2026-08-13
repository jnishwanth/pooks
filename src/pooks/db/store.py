"""SQLite persistence. Plain sqlite3 — the working set is ~634 in-stock rows."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pooks.enrich.quality import MAX_REFRESH_ATTEMPTS
from pooks.models import EventType, Product, utcnow

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add
# them to an existing database, so they are applied explicitly.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("enrichment", "scarcity_has_new", "INTEGER"),
    ("enrichment", "in_price_paise", "INTEGER"),
    ("enrichment", "in_price_source", "TEXT"),
    ("enrichment", "in_available", "INTEGER"),
    ("enrichment", "in_price_unknown", "INTEGER"),
    # Retry budget for the repair pass, so a book nobody has ever rated stops
    # consuming third-party traffic forever.
    ("enrichment", "refresh_attempts", "INTEGER DEFAULT 0"),
    ("enrichment", "tags_json", "TEXT"),
)

# Ratings are rounded to 2dp on construction, but that was added after the first
# rows were written and `enrich.pipeline.merge` carries a stored rating forward
# verbatim — so a legacy 4.063492063492063 could never be corrected by a
# re-enrich and rendered in full on every card. One spelling of "still wrong",
# because it is both the probe and the repair's WHERE.
_UNROUNDED_RATING = "rating IS NOT NULL AND rating != ROUND(rating, 2)"

# Repairs to values already written, as opposed to the column additions above,
# as (probe, repair) pairs. The repair runs only when the probe finds a row:
# `serve.app._open` calls `connect()` — and therefore `_migrate` — inside every
# HTTP request, so an unconditional UPDATE would take the WAL writer lock on
# every dashboard page load, against the same database the daemon is writing to,
# even in the overwhelmingly common case where it rewrites nothing.
_DATA_MIGRATIONS: tuple[tuple[str, str], ...] = (
    (
        f"SELECT 1 FROM enrichment WHERE {_UNROUNDED_RATING} LIMIT 1",
        f"UPDATE enrichment SET rating = ROUND(rating, 2) WHERE {_UNROUNDED_RATING}",
    ),
)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate(conn)
    return conn


def _sql_limit(limit: int | None) -> int:
    """How "all rows" is spelled for a LIMIT parameter.

    SQLite reads a negative LIMIT as no limit, so an optional cap needs no
    conditionally-built clause. Every query below that takes a `limit` defaults
    to None: a cap chosen to be "big enough" is a silent truncation waiting for
    the catalogue to outgrow it.
    """
    return -1 if limit is None else limit


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, column_type in _MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    for probe, repair in _DATA_MIGRATIONS:
        if conn.execute(probe).fetchone() is not None:
            conn.execute(repair)
    conn.commit()


def _row_from_product(product: Product) -> dict[str, Any]:
    """A `Product` as the `products` table stores it, keyed by column name.

    Every column holding a listing is named after the field it stores — a
    correspondence `ingest.diff._metadata_delta` already depends on when it
    compares a stored row against a freshly fetched product — so both
    directions are derived from the model instead of repeating its field list
    in the insert, the conflict update and the inverse below.
    """
    row: dict[str, Any] = product.model_dump()
    # The three exceptions: `categories` is JSON-encoded under a column of its
    # own name plus a suffix, SQLite has no boolean, and `book_key` is derived
    # rather than carried on the model.
    row["categories_json"] = json.dumps(row.pop("categories"))
    row["in_stock"] = int(product.in_stock)
    row["book_key"] = product.book_key
    return row


def product_from_row(row: sqlite3.Row) -> Product:
    """Rebuild a `Product` from its `products` row — the inverse of the upsert.

    Derived from the model so the two directions cannot drift: a field added to
    `Product` with no column behind it fails loudly here rather than being
    silently dropped on every read, which is the failure mode
    `tests/test_cache_roundtrip.py` exists to catch on the enrichment side.
    """
    fields = {name: row[name] for name in Product.model_fields if name != "categories"}
    return Product(categories=json.loads(row["categories_json"] or "[]"), **fields)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---------------------------------------------------------------- products

    def get_product(self, product_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()

    def get_products(self, product_ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        ids = list(product_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM products WHERE product_id IN ({placeholders})", ids
        ).fetchall()
        return {row["product_id"]: row for row in rows}

    def upsert_product(self, product: Product) -> None:
        """Insert or update a listing, preserving what the payload cannot know.

        `first_seen_at` is written once and never updated. The two date columns
        are COALESCEd because the Store API omits them entirely — every sweep
        carries None, so a plain assignment would blank out whatever
        `ingest.backfill_dates` had filled in from wp/v2.
        """
        row = _row_from_product(product)
        columns = [*row, "first_seen_at", "last_seen_at"]
        assignments = [
            f"{col} = excluded.{col}"
            for col in row
            if col not in ("product_id", "date_created", "date_modified")
        ]
        assignments += [
            "date_created = COALESCE(excluded.date_created, products.date_created)",
            "date_modified = COALESCE(excluded.date_modified, products.date_modified)",
            "last_seen_at = excluded.last_seen_at",
        ]
        now = utcnow()
        self.conn.execute(
            f"""
            INSERT INTO products ({", ".join(columns)})
            VALUES ({", ".join("?" * len(columns))})
            ON CONFLICT (product_id) DO UPDATE SET {", ".join(assignments)}
            """,
            [*row.values(), now, now],
        )

    def is_empty(self) -> bool:
        """True when no product has ever been recorded — i.e. a cold start."""
        return self.conn.execute("SELECT 1 FROM products LIMIT 1").fetchone() is None

    def in_stock_products(
        self, limit: int | None = None, *, missing_enrichment: bool = False
    ) -> list[sqlite3.Row]:
        """Buyable listings, newest first.

        `missing_enrichment` narrows it to books nothing has been fetched for
        yet, which is what `pooks enrich` wants without --force; a rescore wants
        the whole in-stock set.
        """
        return self.conn.execute(
            """
            SELECT p.* FROM products p
            LEFT JOIN enrichment e ON e.book_key = p.book_key
            WHERE p.in_stock = 1 AND (e.book_key IS NULL OR NOT ?)
            ORDER BY p.product_id DESC LIMIT ?
            """,
            (int(missing_enrichment), _sql_limit(limit)),
        ).fetchall()

    def product_counts(self) -> dict[str, int]:
        """Headline totals for the status views, in one round trip."""
        row = self.conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM products) AS tracked,
                   (SELECT COUNT(*) FROM products WHERE in_stock = 1) AS in_stock,
                   (SELECT COUNT(*) FROM scores) AS scored
            """
        ).fetchone()
        return dict(row)

    def known_in_stock_ids(self) -> set[int]:
        rows = self.conn.execute("SELECT product_id FROM products WHERE in_stock = 1").fetchall()
        return {row["product_id"] for row in rows}

    def mark_out_of_stock(self, product_ids: Iterable[int]) -> None:
        ids = [(pid,) for pid in product_ids]
        if ids:
            self.conn.executemany(
                "UPDATE products SET in_stock = 0, last_seen_at = datetime('now') "
                "WHERE product_id = ?",
                ids,
            )

    # ------------------------------------------------------------------ events

    def record_event(
        self,
        product_id: int,
        event_type: EventType,
        details: dict[str, Any] | None = None,
        *,
        requires_enrichment: bool,
        requires_inference: bool,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO events (
                product_id, event_type, details_json,
                requires_enrichment, requires_inference, created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                product_id,
                str(event_type),
                json.dumps(details or {}),
                int(requires_enrichment),
                int(requires_inference),
                utcnow(),
            ),
        )

    def pending_event_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) n FROM events WHERE processed_at IS NULL"
            ).fetchone()["n"]
        )

    def unprocessed_events(self, limit: int | None = None) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE processed_at IS NULL ORDER BY id LIMIT ?",
            (_sql_limit(limit),),
        ).fetchall()

    def mark_events_processed(self, event_ids: Iterable[int]) -> None:
        ids = [(utcnow(), eid) for eid in event_ids]
        if ids:
            self.conn.executemany("UPDATE events SET processed_at = ? WHERE id = ?", ids)

    def event_counts_by_type(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT event_type, COUNT(*) n FROM events GROUP BY event_type ORDER BY n DESC"
        ).fetchall()

    def events_for_product(self, product_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE product_id = ? ORDER BY id DESC LIMIT ?",
            (product_id, _sql_limit(limit)),
        ).fetchall()

    # -------------------------------------------------------------- poll state

    def poll_state(self) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM poll_state WHERE id = 1").fetchone()
        if row is None:  # pragma: no cover - schema seeds this row
            self.conn.execute("INSERT INTO poll_state (id) VALUES (1)")
            row = self.conn.execute("SELECT * FROM poll_state WHERE id = 1").fetchone()
        return row

    def update_poll_state(self, **fields: Any) -> None:
        allowed = {
            "last_modified",
            "last_max_product_id",
            "last_instock_total",
            "last_poll_at",
            "last_sweep_at",
            "last_304_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k} = ?" for k in updates)
        self.conn.execute(
            f"UPDATE poll_state SET {assignments} WHERE id = 1", list(updates.values())
        )

    # -------------------------------------------------------------- enrichment

    def get_enrichment(self, book_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM enrichment WHERE book_key = ?", (book_key,)
        ).fetchone()

    def put_enrichment(self, book_key: str, data: dict[str, Any]) -> None:
        columns = [
            "isbn",
            "resolved_title",
            "resolved_author",
            "rating",
            "ratings_count",
            "rating_source",
            "provenance_json",
            "in_print",
            "comp_listing_count",
            "scarcity_has_new",
            "in_price_paise",
            "in_price_source",
            "in_available",
            "in_price_unknown",
            "tags_json",
            "synopsis",
            "match_method",
            "expires_at",
            "refresh_attempts",
        ]
        values = [data.get(col) for col in columns]
        assignments = ", ".join(f"{col} = excluded.{col}" for col in columns)
        self.conn.execute(
            f"""
            INSERT INTO enrichment (book_key, {", ".join(columns)}, fetched_at)
            VALUES ({", ".join("?" * (len(columns) + 2))})
            ON CONFLICT (book_key) DO UPDATE SET {assignments}, fetched_at = excluded.fetched_at
            """,
            [book_key, *values, utcnow()],
        )

    def put_tags(self, book_key: str, tags: dict[str, list[str]] | None) -> None:
        """Write just the tag list, leaving the rest of the record alone.

        `put_enrichment` overwrites every column, so a repair that has only a
        tag list to store would otherwise have to re-fetch a whole record to
        avoid blanking one. None stays NULL — "never answered", which is what
        keeps the book eligible for another attempt — rather than becoming the
        `{}` that means Hardcover replied and has none.
        """
        self.conn.execute(
            "UPDATE enrichment SET tags_json = ? WHERE book_key = ?",
            (None if tags is None else json.dumps(tags), book_key),
        )

    # --------------------------------------------------------------- llm cache

    def get_llm(self, book_key: str, role: str, prompt_version: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT response_json FROM llm_cache "
            "WHERE book_key = ? AND role = ? AND prompt_version = ?",
            (book_key, role, prompt_version),
        ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def get_llm_many(
        self, book_keys: Iterable[str], role: str, prompt_version: int
    ) -> dict[str, dict[str, Any]]:
        """One query for a whole page's worth of cached responses.

        The dashboard loads the whole in-stock list at once; per-book `get_llm`
        calls made that a query per row.
        """
        keys = list(book_keys)
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = self.conn.execute(
            "SELECT book_key, response_json FROM llm_cache "
            f"WHERE role = ? AND prompt_version = ? AND book_key IN ({placeholders})",
            [role, prompt_version, *keys],
        ).fetchall()
        return {row["book_key"]: json.loads(row["response_json"]) for row in rows}

    def put_llm(
        self,
        book_key: str,
        role: str,
        prompt_version: int,
        response: dict[str, Any],
        model: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO llm_cache (book_key, role, prompt_version, response_json, model, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT (book_key, role, prompt_version) DO UPDATE SET
                response_json = excluded.response_json,
                model = excluded.model,
                created_at = excluded.created_at
            """,
            (book_key, role, prompt_version, json.dumps(response), model, utcnow()),
        )

    # ------------------------------------------------------------------ scores

    def put_score(self, product_id: int, breakdown: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO scores (
                product_id, score, quality, renown, value,
                condition_factor, confidence, breakdown_json, computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (product_id) DO UPDATE SET
                score = excluded.score,
                quality = excluded.quality,
                renown = excluded.renown,
                value = excluded.value,
                condition_factor = excluded.condition_factor,
                confidence = excluded.confidence,
                breakdown_json = excluded.breakdown_json,
                computed_at = excluded.computed_at
            """,
            (
                product_id,
                breakdown["score"],
                breakdown.get("quality"),
                breakdown.get("renown"),
                breakdown.get("value"),
                breakdown.get("condition_factor"),
                breakdown.get("confidence"),
                json.dumps(breakdown),
                utcnow(),
            ),
        )

    def ranked_in_stock(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Buyable listings, best score first. `limit=None` returns all of them."""
        return self.conn.execute(
            """
            SELECT p.*, s.score, s.quality, s.renown, s.value,
                   s.confidence, s.breakdown_json, e.rating, e.ratings_count,
                   -- p.* already carries book_key; naming it here documents that
                   -- callers can join blurbs without re-querying per row.
                   e.rating_source, e.resolved_author, e.in_print, e.tags_json,
                   e.comp_listing_count,
                   e.in_price_paise, e.in_price_source, e.in_available, e.in_price_unknown
            FROM products p
            LEFT JOIN scores s ON s.product_id = p.product_id
            LEFT JOIN enrichment e ON e.book_key = p.book_key
            WHERE p.in_stock = 1
            ORDER BY COALESCE(s.score, -1) DESC
            LIMIT ?
            """,
            (_sql_limit(limit),),
        ).fetchall()

    def improvable_books(
        self,
        primary_rating_source: str | None,
        *,
        tags_askable: bool,
        limit: int | None = None,
        min_score: float = 0.0,
    ) -> list[sqlite3.Row]:
        """In-stock books whose enrichment could plausibly be bettered.

        `primary_rating_source` is `Config.primary_rating_source`: a rating from
        anywhere else is a fallback, and a fallback is worth re-fetching.

        `tags_askable` is `Config.tags_askable`. A book whose only defect is
        that Hardcover was never successfully asked matches no other predicate
        here — it has a primary rating and an amazon.in price — so without this
        term `improvable`'s "tags never fetched" reason could never be reached
        and those tags stayed empty forever. It is conditional because with no
        key to ask with the gap is not one a refresh can close, and offering
        every enriched book up would spend the whole retry budget proving it.

        `min_score` gates the *expensive* repair alone. Re-running the chain
        costs Goodreads' 60s and Amazon's 90s, which is wasted on a book that
        cannot clear the push threshold; a tag list is one Hardcover call paced
        at a second, and tags are a browsing filter rather than a scoring or
        push input, so the reason the floor exists does not apply to them.
        Every row therefore carries the floor's verdict as `full_refresh_ok`:
        the caller chooses between the two repairs, and a row admitted only by
        the tags branch must not be handed to the chain.

        In-stock only: an unbuyable book cannot reach the digest, so upgrading
        it is third-party traffic spent for nothing.

        Ordered worst-first so the repair converges where it matters — a blocked
        price is a hole, a fallback source is merely second-best — and within
        that by score, so the books at the top of the ranking become correct
        first.
        """
        return self.conn.execute(
            """
            SELECT p.*, e.rating_source, e.in_price_source, e.in_price_unknown,
                   e.in_available, e.provenance_json, e.tags_json, s.score,
                   -- An unscored book has not been through the pipeline yet, so
                   -- it gets the benefit of the doubt; a scored one below the
                   -- floor cannot be pushed, and a 90s Amazon lookup on it is
                   -- wasted. SQLite lets the WHERE clause below read this
                   -- alias, so the rule has one spelling for both readers.
                   (s.score IS NULL OR s.score >= :min_score) AS full_refresh_ok
            FROM products p
            JOIN enrichment e ON e.book_key = p.book_key
            LEFT JOIN scores s ON s.product_id = p.product_id
            WHERE p.in_stock = 1
              AND COALESCE(e.refresh_attempts, 0) < :max_attempts
              AND (
                    (
                      full_refresh_ok
                      AND (
                            e.in_price_unknown = 1
                         OR e.rating_source IS NULL
                         OR e.rating_source != :primary_rating_source
                         OR e.in_price_source IS NULL
                         OR e.in_price_source != 'amazon.in'
                      )
                    )
                 -- NULL is "never answered"; '{}' is "asked, and has none",
                 -- which is settled for roughly two books in five. Bounded by
                 -- the retry budget alone: the floor above buys nothing here,
                 -- and applying it left a low-scoring book untagged forever.
                 OR (:tags_askable AND e.tags_json IS NULL)
              )
            ORDER BY
              CASE WHEN e.in_price_unknown = 1 THEN 0
                   WHEN e.rating_source IS NULL THEN 1
                   WHEN e.in_price_source IS NULL THEN 2
                   ELSE 3 END,
              COALESCE(s.score, 0) DESC
            LIMIT :limit
            """,
            {
                "max_attempts": MAX_REFRESH_ATTEMPTS,
                "min_score": min_score,
                "primary_rating_source": primary_rating_source,
                "tags_askable": int(tags_askable),
                "limit": _sql_limit(limit),
            },
        ).fetchall()

    def previous_price_paise(self, book_key: str, exclude_product_id: int) -> int | None:
        """Cheapest price this book was previously listed at, under any listing.

        Relists get a *new* product id, so a same-product PRICE_CHANGE almost
        never fires; the meaningful comparison is across listings of the same
        book. Retaining sold-out rows is what makes it possible.
        """
        row = self.conn.execute(
            "SELECT MIN(price_paise) p FROM products "
            "WHERE book_key = ? AND product_id != ? AND price_paise IS NOT NULL",
            (book_key, exclude_product_id),
        ).fetchone()
        return row["p"] if row and row["p"] is not None else None

    def prune_unbacked_scores(self) -> int:
        """Drop scores whose enrichment has gone.

        Without this, a score computed under an older scoring function lingers
        indefinitely for any book that is no longer re-scored, and `top` and
        `calibrate` silently mix the two.
        """
        cursor = self.conn.execute(
            """
            DELETE FROM scores WHERE product_id IN (
              SELECT s.product_id FROM scores s
              JOIN products p ON p.product_id = s.product_id
              LEFT JOIN enrichment e ON e.book_key = p.book_key
              WHERE e.book_key IS NULL)
            """
        )
        return cursor.rowcount or 0

    def prune_orphaned_enrichment(self) -> int:
        """Drop enrichment no product references any more.

        `book_key` for an ISBN-less book is derived from title and author, so
        recovering a missing author changes the key and strands the old row.
        The new key is the better one — this just clears what it left behind.
        """
        cursor = self.conn.execute(
            "DELETE FROM enrichment WHERE book_key NOT IN (SELECT book_key FROM products)"
        )
        return cursor.rowcount or 0

    def bump_refresh_attempt(self, book_key: str) -> None:
        self.conn.execute(
            "UPDATE enrichment SET refresh_attempts = COALESCE(refresh_attempts, 0) + 1 "
            "WHERE book_key = ?",
            (book_key,),
        )

    # ----------------------------------------------------------- notifications

    def already_notified(self, product_id: int, event_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM notifications WHERE product_id = ? AND event_id = ?",
                (product_id, event_id),
            ).fetchone()
            is not None
        )

    def record_notification(self, product_id: int, event_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO notifications (product_id, event_id, sent_at) VALUES (?,?,?)",
            (product_id, event_id, utcnow()),
        )
