"""Local dashboard.

Read-only over the SQLite the pipeline writes, so it can run alongside the
scheduler without coordination.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator
from rapidfuzz import fuzz

from pooks.config import Config, load_config
from pooks.db.store import Store, connect
from pooks.enrich.sources import TAG_FACETS, flatten_tags, parse_tags_json
from pooks.llm.roles import Role

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="pooks", docs_url=None, redoc_url=None)

# A filter that is not set renders as `value=""` in the form, so a browser
# submits `min_rating=&min_ratings_count=` alongside every search. FastAPI
# cannot coerce `""` to a number and rejected the whole request with 422
# ("Input should be a valid number"), which read as the *search box* wanting a
# number rather than text — in practice no search from the form ever worked.
_NO_FILTER = 0
_PAGE_SIZE = 100


def _blank_as(default: Any) -> BeforeValidator:
    """Read an empty query parameter as an absent one.

    Substituting the default rather than None is deliberate: an optional
    `float | None` carrying a `ge` constraint raises *inside* pydantic when the
    value is None, which would trade the 422 for a 500.
    """

    def coerce(value: Any) -> Any:
        return default if isinstance(value, str) and not value.strip() else value

    return BeforeValidator(coerce)


def _clean(values: list[str], *, lower: bool = True) -> tuple[str, ...]:
    """Deduplicate a repeatable filter parameter, preserving the order given.

    A browser resubmitting a form happily sends the same chip twice, and a
    duplicate in an `all`-mode tag filter is harmless but a duplicate chip in
    the rendered panel is not.
    """
    seen: list[str] = []
    for value in values:
        cleaned = value.strip().lower() if lower else value.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


Limit = Annotated[int, _blank_as(_PAGE_SIZE), Query(ge=1)]
Offset = Annotated[int, _blank_as(0), Query(ge=0)]
AddedWithinDays = Annotated[int, _blank_as(_NO_FILTER), Query(ge=0)]
MinRating = Annotated[float, _blank_as(_NO_FILTER), Query(ge=0.0, le=5.0)]
MinRatingsCount = Annotated[int, _blank_as(_NO_FILTER), Query(ge=0)]
MinConfidence = Annotated[float, _blank_as(_NO_FILTER), Query(ge=0.0, le=1.0)]
# The checkbox is absent from a form submission when unticked, but `?unscored=`
# is hand-typeable and failed the same way the numeric filters did.
Unscored = Annotated[bool, _blank_as(False), Query()]


def _query(filters: Filters, limit: int, offset: int) -> list[tuple[str, str]]:
    """The current view as query parameters, omitting anything at its default.

    Every link the page renders is built from this, so a chip, a sort or a page
    button carries the rest of the filter state with it. Clicking a genre used
    to drop the search that found it, because the link was a bare `?tag=`.
    """
    params: list[tuple[str, str]] = []
    if filters.q:
        params.append(("q", filters.q))
    params += [("tag", tag) for tag in filters.tags]
    params += [("exclude_tag", tag) for tag in filters.exclude_tags]
    if filters.tag_mode != "any":
        params.append(("tag_mode", filters.tag_mode))
    params += [("category", name) for name in filters.categories]
    params += [("exclude_category", name) for name in filters.exclude_categories]
    if filters.min_rating:
        params.append(("min_rating", str(filters.min_rating)))
    if filters.min_ratings_count:
        params.append(("min_ratings_count", str(filters.min_ratings_count)))
    if filters.min_confidence:
        params.append(("min_confidence", str(filters.min_confidence)))
    if filters.unscored:
        params.append(("unscored", "true"))
    if filters.added_within_days:
        params.append(("added_within_days", str(filters.added_within_days)))
    if filters.sort != "score":
        params.append(("sort", filters.sort))
    if limit != _PAGE_SIZE:
        params.append(("limit", str(limit)))
    if offset:
        params.append(("offset", str(offset)))
    return params


def _with(filters: Filters, limit: int, offset: int = 0, **changes: Any) -> str:
    """The current URL with some parameters replaced."""
    params = _query(filters, limit, offset)
    for key, value in changes.items():
        params = [(k, v) for k, v in params if k != key]
        if value not in (None, "", 0, False):
            params.append((key, "true" if value is True else str(value)))
    return f"/?{urlencode(params)}" if params else "/"


def _toggle(filters: Filters, limit: int, key: str, value: str) -> str:
    """The current URL with one repeatable filter value added or removed.

    Paging is dropped rather than carried: page four of the previous result set
    is a different set of books, and usually an empty page.
    """
    params = _query(filters, limit, 0)
    pair = (key, value)
    params = [p for p in params if p != pair] if pair in params else [*params, pair]
    return f"/?{urlencode(params)}" if params else "/"


TEMPLATES.env.globals["with_params"] = _with
TEMPLATES.env.globals["toggle"] = _toggle


def _open() -> tuple[Config, Store]:
    config = load_config()
    return config, Store(connect(config.db_path))


def _load_books(store: Store, config: Config) -> list[dict[str, Any]]:
    """The whole ranked in-stock list, blurbs attached.

    Deliberately unlimited: filtering and paging both happen in Python below,
    so truncating here would hide a book from a *search*, not merely from the
    first page. It was capped at a hardcoded 634 — the catalogue size on the
    day it was written — which would have started silently dropping books the
    moment the shop grew.
    """
    books = _rows_to_books(store.ranked_in_stock())
    _attach_blurbs(store, books, config.prompt_version)
    _attach_sources(store, books)
    return books


def _rows_to_books(rows: list[Any]) -> list[dict[str, Any]]:
    books = []
    for row in rows:
        breakdown = json.loads(row["breakdown_json"] or "{}")
        grouped = parse_tags_json(row["tags_json"])
        # The shop's own arrival date where wp/v2 supplied one, otherwise when
        # this pipeline first saw the listing. They are different facts — a book
        # first seen yesterday may have sat on the shelf for months — so the
        # fallback is flagged rather than passed off as the real thing.
        added = row["date_created"] or row["first_seen_at"]
        books.append(
            {
                "added": added,
                "added_estimated": row["date_created"] is None,
                "tag_facets": grouped,
                "product_id": row["product_id"],
                "book_key": row["book_key"],
                "name": row["name"],
                "author": row["author"] or row["resolved_author"],
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
                "confidence": row["confidence"],
                "rating": row["rating"],
                "ratings_count": row["ratings_count"],
                "rating_source": row["rating_source"],
                "in_print": row["in_print"],
                "india_inr": (row["in_price_paise"] / 100) if row["in_price_paise"] else None,
                "india_source": row["in_price_source"],
                "india_available": row["in_available"],
                "india_unknown": row["in_price_unknown"],
                "tags": flatten_tags(grouped),
                "comp_listings": row["comp_listing_count"],
                "notes": breakdown.get("notes", {}),
                "blurb": None,
                "sources": {},
            }
        )
    return books


def _attach_blurbs(store: Store, books: list[dict[str, Any]], version: int) -> None:
    """Fetch every blurb in one query.

    `ranked_in_stock` already selects book_key, so the previous version's
    per-book lookup of it was pure waste: two queries per book meant ~200 for a
    100-book page.
    """
    keys = [book["book_key"] for book in books if book.get("book_key")]
    by_key = store.get_llm_many(keys, Role.BLURB, version)
    for book in books:
        key = book.get("book_key")
        if key and (payload := by_key.get(key)):
            book["blurb"] = payload.get("blurb")


@dataclass(frozen=True)
class Filters:
    """Everything the browse view can narrow by.

    One object rather than a dozen parameters because the same values are read
    three times per request — to filter, to count the facets, and to render the
    form back with its state — and the third used to be a hand-built dict that
    could disagree with the first two.
    """

    q: str = ""
    tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    tag_mode: str = "any"
    categories: tuple[str, ...] = ()
    exclude_categories: tuple[str, ...] = ()
    min_rating: float = 0.0
    min_ratings_count: int = 0
    min_confidence: float = 0.0
    unscored: bool = False
    added_within_days: int = 0
    sort: str = "score"

    @property
    def searching(self) -> bool:
        return bool(self.q.strip())

    @property
    def any_active(self) -> bool:
        """Whether anything is narrowing the list, for the "clear" affordance."""
        return bool(
            self.searching
            or self.tags
            or self.exclude_tags
            or self.categories
            or self.exclude_categories
            or self.min_rating
            or self.min_ratings_count
            or self.added_within_days
        )


# Books with no known arrival date sort last rather than crashing the compare.
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Sort:
    label: str
    key: Callable[[dict[str, Any]], Any]
    descending: bool


# `score` first: it is the default, and reproduces the order `ranked_in_stock`
# already returns, so the unsorted page is unchanged.
SORTS: dict[str, Sort] = {
    "score": Sort("best first", lambda b: b["score"] if b["score"] is not None else -1.0, True),
    "added": Sort("newest first", lambda b: _added_at(b) or _EPOCH, True),
    "price": Sort("cheapest first", lambda b: b["price_inr"], False),
    "rating": Sort("highest rated", lambda b: b["rating"] or 0.0, True),
    "ratings_count": Sort("most rated", lambda b: b["ratings_count"] or 0, True),
}


def _added_at(book: dict[str, Any]) -> datetime | None:
    """When the book arrived, as a comparable instant.

    Sorting or windowing on the raw string is wrong: `date_created` comes from
    wp/v2 as a naive GMT stamp while `first_seen_at` carries an offset, so they
    do not order lexically against each other.
    """
    raw = book.get("added")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _matches_tags(book: dict[str, Any], filters: Filters) -> bool:
    tags = set(book["tags"])
    if filters.exclude_tags and tags & set(filters.exclude_tags):
        return False
    if not filters.tags:
        return True
    # `all` narrows to books carrying every selected tag, which is how you get
    # from "fantasy" to something specific; `any` widens, which is how you ask
    # for two moods you would be happy with either of.
    if filters.tag_mode == "all":
        return set(filters.tags) <= tags
    return bool(tags & set(filters.tags))


def _matches_categories(book: dict[str, Any], filters: Filters) -> bool:
    categories = {c.lower() for c in book["categories"]}
    if filters.exclude_categories and categories & {c.lower() for c in filters.exclude_categories}:
        return False
    if not filters.categories:
        return True
    return bool(categories & {c.lower() for c in filters.categories})


def _attach_sources(store: Store, books: list[dict[str, Any]]) -> None:
    """What each source said about each book, for the provenance panel.

    The merged row shows the winner; this shows the field the winner beat, and
    whether a better source was ever asked at all — which is the question the
    merged row cannot answer and the repair pass turns on.
    """
    ledgers = store.observations_many([b["book_key"] for b in books if b.get("book_key")])
    for book in books:
        grouped: dict[str, dict[str, Any]] = {}
        for row in ledgers.get(book["book_key"], []):
            try:
                grouped.setdefault(row["field"], {})[row["source"]] = json.loads(row["value_json"])
            except ValueError:
                continue
        book["sources"] = grouped


def _apply_filters(books: list[dict[str, Any]], filters: Filters) -> list[dict[str, Any]]:
    """Filter and order in Python rather than SQL.

    633 rows is nothing, and it lets `q` be a real fuzzy match via rapidfuzz —
    already a dependency, already used by the matching ladder — so a misspelled
    author still finds the book. SQL LIKE would not.
    """
    # A search is a lookup, not a browse: someone typing an author's name wants
    # to know whether the shop has it, not whether the pipeline has scored it
    # yet. Hiding unscored books here returns a confusing empty result for a
    # book that is plainly in stock.
    if not filters.unscored and not filters.searching:
        books = [b for b in books if b["score"] is not None]
    if not filters.searching:
        books = [b for b in books if (b["confidence"] or 0) >= filters.min_confidence]

    books = [b for b in books if _matches_tags(b, filters)]
    books = [b for b in books if _matches_categories(b, filters)]

    if filters.min_rating:
        books = [b for b in books if (b["rating"] or 0) >= filters.min_rating]
    if filters.min_ratings_count:
        books = [b for b in books if (b["ratings_count"] or 0) >= filters.min_ratings_count]

    if filters.added_within_days:
        cutoff = datetime.now(UTC) - timedelta(days=filters.added_within_days)
        # A book with no known arrival date is excluded rather than assumed
        # recent: the whole point of the window is "what is new".
        books = [b for b in books if (added := _added_at(b)) is not None and added >= cutoff]

    order = SORTS.get(filters.sort, SORTS["score"])
    books = sorted(books, key=order.key, reverse=order.descending)

    if query := filters.q.strip():
        scored = []
        for book in books:
            haystack = f"{book['name']} {book['author'] or ''}"
            score = fuzz.partial_token_set_ratio(query.lower(), haystack.lower())
            if score >= 75:
                scored.append((score, book))
        # Sorted after the chosen ordering and stably, so search narrows the
        # list rather than replacing it with a relevance ordering.
        books = [b for _, b in sorted(scored, key=lambda pair: -pair[0])]

    return books


def _facet_counts(
    catalogue: list[dict[str, Any]], shown: list[dict[str, Any]], active: tuple[str, ...]
) -> dict[str, list[dict[str, Any]]]:
    """Tag counts over the books currently shown, grouped by facet.

    Counted after filtering, so a chip's number is what clicking it would leave
    rather than what the catalogue holds. Which facet a tag belongs to comes
    from the *whole* catalogue, though: an active tag that filtered everything
    out has no rows left to learn its facet from, and dropping it would remove
    the only control that can undo it.
    """
    facet_of: dict[str, str] = {}
    for book in catalogue:
        for facet, tags in book["tag_facets"].items():
            for tag in tags:
                facet_of.setdefault(tag, facet)

    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for book in shown:
        for facet, tags in book["tag_facets"].items():
            counts[facet].update(tags)
    for tag in active:
        counts[facet_of.get(tag, TAG_FACETS[-1])].setdefault(tag, 0)

    ordered = {}
    for facet in TAG_FACETS:
        if facet not in counts:
            continue
        rows = [
            {"tag": tag, "count": n, "active": tag in active}
            for tag, n in counts[facet].most_common()
        ]
        # `most_common` already orders by count; the sort settles ties by name so
        # a page reload cannot reshuffle equally-common chips.
        ordered[facet] = sorted(rows, key=lambda row: (-counts[facet][str(row["tag"])], row["tag"]))
    return ordered


def _category_counts(shown: list[dict[str, Any]], active: tuple[str, ...]) -> list[dict[str, Any]]:
    """Shop categories, which unlike Hardcover tags cover the whole catalogue.

    That coverage is the point: tags reach roughly three books in five, so a
    category is the only filter that can be relied on to answer "not comics".
    """
    counts: Counter[str] = Counter()
    for book in shown:
        counts.update(book["categories"])
    lowered = {c.lower() for c in active}
    for category in active:
        counts.setdefault(category, 0)
    rows = [
        {"category": c, "count": n, "active": c.lower() in lowered} for c, n in counts.most_common()
    ]
    return sorted(rows, key=lambda row: (-counts[str(row["category"])], row["category"]))


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    limit: Limit = _PAGE_SIZE,
    offset: Offset = 0,
    min_confidence: MinConfidence = _NO_FILTER,
    unscored: Unscored = False,
    q: str = Query(default=""),
    tag: Annotated[list[str] | None, Query()] = None,
    exclude_tag: Annotated[list[str] | None, Query()] = None,
    tag_mode: str = Query(default="any", pattern="^(any|all)$"),
    category: Annotated[list[str] | None, Query()] = None,
    exclude_category: Annotated[list[str] | None, Query()] = None,
    min_rating: MinRating = _NO_FILTER,
    min_ratings_count: MinRatingsCount = _NO_FILTER,
    added_within_days: AddedWithinDays = _NO_FILTER,
    sort: str = Query(default="score"),
) -> HTMLResponse:
    config, store = _open()
    # Filters apply across the whole in-stock list, not just the first page,
    # so a narrow search still finds a book ranked 400th.
    catalogue = _load_books(store, config)

    filters = Filters(
        q=q,
        tags=_clean(tag or []),
        exclude_tags=_clean(exclude_tag or []),
        tag_mode=tag_mode,
        categories=_clean(category or [], lower=False),
        exclude_categories=_clean(exclude_category or [], lower=False),
        min_rating=min_rating,
        min_ratings_count=min_ratings_count,
        min_confidence=min_confidence,
        unscored=unscored,
        added_within_days=added_within_days,
        sort=sort if sort in SORTS else "score",
    )
    matched = _apply_filters(catalogue, filters)
    state = store.poll_state()
    counts = store.product_counts()

    return TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "books": matched[offset : offset + limit],
            "facets": _facet_counts(catalogue, matched, filters.tags),
            "categories": _category_counts(matched, filters.categories),
            "sorts": {key: order.label for key, order in SORTS.items()},
            "page": {
                "offset": offset,
                "limit": limit,
                "matched": len(matched),
                "has_prev": offset > 0,
                "has_next": offset + limit < len(matched),
            },
            "stats": {
                "tracked": counts["tracked"],
                "in_stock": counts["in_stock"],
                "scored": counts["scored"],
                "last_sweep": state["last_sweep_at"] or "never",
                "last_poll": state["last_poll_at"] or "never",
            },
            "filters": filters,
        },
    )


@app.get("/api/books")
async def api_books(
    limit: Limit = _PAGE_SIZE,
    offset: Offset = 0,
    q: str = Query(default=""),
    tag: Annotated[list[str] | None, Query()] = None,
    exclude_tag: Annotated[list[str] | None, Query()] = None,
    tag_mode: str = Query(default="any", pattern="^(any|all)$"),
    category: Annotated[list[str] | None, Query()] = None,
    exclude_category: Annotated[list[str] | None, Query()] = None,
    min_rating: MinRating = _NO_FILTER,
    min_ratings_count: MinRatingsCount = _NO_FILTER,
    added_within_days: AddedWithinDays = _NO_FILTER,
    sort: str = Query(default="score"),
) -> JSONResponse:
    config, store = _open()
    books = _apply_filters(
        _load_books(store, config),
        Filters(
            q=q,
            tags=_clean(tag or []),
            exclude_tags=_clean(exclude_tag or []),
            tag_mode=tag_mode,
            categories=_clean(category or [], lower=False),
            exclude_categories=_clean(exclude_category or [], lower=False),
            min_rating=min_rating,
            min_ratings_count=min_ratings_count,
            min_confidence=0.0,
            unscored=True,
            added_within_days=added_within_days,
            sort=sort if sort in SORTS else "score",
        ),
    )
    return JSONResponse(books[offset : offset + limit])


@app.get("/api/health")
async def health() -> JSONResponse:
    _, store = _open()
    state = store.poll_state()
    return JSONResponse(
        {
            "ok": True,
            "last_poll_at": state["last_poll_at"],
            "last_sweep_at": state["last_sweep_at"],
            "pending_events": store.pending_event_count(),
        }
    )
