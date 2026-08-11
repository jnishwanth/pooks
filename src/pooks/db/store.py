"""SQLite persistence. Plain sqlite3 — the working set is ~634 in-stock rows."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pooks.models import EventType, Product, utcnow

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add
# them to an existing database, so they are applied explicitly.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("enrichment", "fx_rate", "REAL"),
    ("enrichment", "scarcity_has_new", "INTEGER"),
    ("enrichment", "in_price_paise", "INTEGER"),
    ("enrichment", "in_price_source", "TEXT"),
    ("enrichment", "in_price_url", "TEXT"),
    ("enrichment", "in_available", "INTEGER"),
    ("enrichment", "in_price_unknown", "INTEGER"),
)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, column_type in _MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    conn.commit()


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
        now = utcnow()
        self.conn.execute(
            """
            INSERT INTO products (
                product_id, book_key, name, slug, permalink, isbn, author, publisher,
                book_format, pages, condition, categories_json, price_paise,
                regular_price_paise, in_stock, date_created, date_modified,
                first_seen_at, last_seen_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (product_id) DO UPDATE SET
                book_key = excluded.book_key,
                name = excluded.name,
                slug = excluded.slug,
                permalink = excluded.permalink,
                isbn = excluded.isbn,
                author = excluded.author,
                publisher = excluded.publisher,
                book_format = excluded.book_format,
                pages = excluded.pages,
                condition = excluded.condition,
                categories_json = excluded.categories_json,
                price_paise = excluded.price_paise,
                regular_price_paise = excluded.regular_price_paise,
                in_stock = excluded.in_stock,
                date_created = COALESCE(excluded.date_created, products.date_created),
                date_modified = COALESCE(excluded.date_modified, products.date_modified),
                last_seen_at = excluded.last_seen_at
            """,
            (
                product.product_id,
                product.book_key,
                product.name,
                product.slug,
                product.permalink,
                product.isbn,
                product.author,
                product.publisher,
                product.book_format,
                product.pages,
                product.condition,
                json.dumps(product.categories),
                product.price_paise,
                product.regular_price_paise,
                int(product.in_stock),
                product.date_created,
                product.date_modified,
                now,
                now,
            ),
        )

    def is_empty(self) -> bool:
        """True when no product has ever been recorded — i.e. a cold start."""
        return self.conn.execute("SELECT 1 FROM products LIMIT 1").fetchone() is None

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
    ) -> int:
        cursor = self.conn.execute(
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
        return int(cursor.lastrowid or 0)

    def unprocessed_events(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE processed_at IS NULL ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def mark_events_processed(self, event_ids: Iterable[int]) -> None:
        ids = [(utcnow(), eid) for eid in event_ids]
        if ids:
            self.conn.executemany(
                "UPDATE events SET processed_at = ? WHERE id = ?", ids
            )

    def events_for_product(self, product_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE product_id = ? ORDER BY id DESC LIMIT ?",
            (product_id, limit),
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
            "in_price_url",
            "in_available",
            "in_price_unknown",
            "synopsis",
            "match_method",
            "expires_at",
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

    # --------------------------------------------------------------- llm cache

    def get_llm(self, book_key: str, role: str, prompt_version: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT response_json FROM llm_cache "
            "WHERE book_key = ? AND role = ? AND prompt_version = ?",
            (book_key, role, prompt_version),
        ).fetchone()
        return json.loads(row["response_json"]) if row else None

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
                product_id, score, quality, renown, value, affordability,
                condition_factor, confidence, breakdown_json, computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (product_id) DO UPDATE SET
                score = excluded.score,
                quality = excluded.quality,
                renown = excluded.renown,
                value = excluded.value,
                affordability = excluded.affordability,
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
                breakdown.get("affordability"),
                breakdown.get("condition_factor"),
                breakdown.get("confidence"),
                json.dumps(breakdown),
                utcnow(),
            ),
        )

    def ranked_in_stock(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT p.*, s.score, s.quality, s.renown, s.value, s.affordability,
                   s.confidence, s.breakdown_json, e.rating, e.ratings_count,
                   e.rating_source, e.in_print, e.comp_listing_count,
                   e.in_price_paise, e.in_price_source, e.in_available, e.in_price_unknown
            FROM products p
            LEFT JOIN scores s ON s.product_id = p.product_id
            LEFT JOIN enrichment e ON e.book_key = p.book_key
            WHERE p.in_stock = 1
            ORDER BY COALESCE(s.score, -1) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

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
