"""Local dashboard.

Read-only over the SQLite the pipeline writes, so it can run alongside the
scheduler without coordination.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from pooks.config import load_config
from pooks.db.store import Store, connect

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="pooks", docs_url=None, redoc_url=None)


def _store() -> Store:
    return Store(connect(load_config().db_path))


def _rows_to_books(rows: list[Any]) -> list[dict[str, Any]]:
    books = []
    for row in rows:
        breakdown = json.loads(row["breakdown_json"] or "{}")
        books.append(
            {
                "product_id": row["product_id"],
                "name": row["name"],
                "author": row["author"],
                "permalink": row["permalink"],
                "isbn": row["isbn"],
                "condition": row["condition"],
                "publisher": row["publisher"],
                "categories": json.loads(row["categories_json"] or "[]"),
                "price_inr": (row["price_paise"] or 0) / 100,
                "score": row["score"],
                "quality": row["quality"],
                "renown": row["renown"],
                "value": row["value"],
                "affordability": row["affordability"],
                "confidence": row["confidence"],
                "rating": row["rating"],
                "ratings_count": row["ratings_count"],
                "rating_source": row["rating_source"],
                "in_print": row["in_print"],
                "india_inr": (row["in_price_paise"] / 100) if row["in_price_paise"] else None,
                "india_source": row["in_price_source"],
                "india_available": row["in_available"],
                "india_unknown": row["in_price_unknown"],
                "comp_listings": row["comp_listing_count"],
                "notes": breakdown.get("notes", {}),
                "blurb": None,
            }
        )
    return books


def _attach_blurbs(store: Store, books: list[dict[str, Any]]) -> None:
    version = load_config().llm.get("prompt_version", 1)
    for book in books:
        row = store.conn.execute(
            "SELECT book_key FROM products WHERE product_id = ?", (book["product_id"],)
        ).fetchone()
        if row is None:
            continue
        if payload := store.get_llm(row["book_key"], "blurb", version):
            book["blurb"] = payload.get("blurb")


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    limit: int = Query(default=100, le=634),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    unscored: bool = Query(default=False),
) -> HTMLResponse:
    store = _store()
    books = _rows_to_books(store.ranked_in_stock(limit=limit))
    _attach_blurbs(store, books)

    if not unscored:
        books = [b for b in books if b["score"] is not None]
    books = [b for b in books if (b["confidence"] or 0) >= min_confidence]

    state = store.poll_state()
    counts = store.conn.execute(
        "SELECT COUNT(*) n, SUM(in_stock) in_stock FROM products"
    ).fetchone()
    scored_total = store.conn.execute(
        "SELECT COUNT(*) n FROM scores"
    ).fetchone()["n"]

    return TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "books": books,
            "stats": {
                "tracked": counts["n"],
                "in_stock": counts["in_stock"] or 0,
                "scored": scored_total,
                "last_sweep": state["last_sweep_at"] or "never",
                "last_poll": state["last_poll_at"] or "never",
            },
            "filters": {"min_confidence": min_confidence, "unscored": unscored},
        },
    )


@app.get("/api/books")
async def api_books(limit: int = Query(default=100, le=634)) -> JSONResponse:
    store = _store()
    books = _rows_to_books(store.ranked_in_stock(limit=limit))
    _attach_blurbs(store, books)
    return JSONResponse(books)


@app.get("/api/health")
async def health() -> JSONResponse:
    store = _store()
    state = store.poll_state()
    return JSONResponse(
        {
            "ok": True,
            "last_poll_at": state["last_poll_at"],
            "last_sweep_at": state["last_sweep_at"],
            "pending_events": store.conn.execute(
                "SELECT COUNT(*) n FROM events WHERE processed_at IS NULL"
            ).fetchone()["n"],
        }
    )
