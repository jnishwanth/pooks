"""Open Library.

Its ratings are near-useless for ranking — recon found 2.33 from 3 ratings for a
work Goodreads rates 4.11 from 7,516 — so it sits last in the chain and is
normally filtered out by `min_ratings_count`.

Its real value here is elsewhere: `want_to_read` shelf counts are a popularity
proxy that survives when no rating is found at all, and `first_publish_year` /
`edition_count` feed the in-print heuristic.
"""

from __future__ import annotations

import logging
from typing import Any

from pooks.enrich.http import PoliteClient
from pooks.enrich.match import verify
from pooks.enrich.sources import RatingResult

log = logging.getLogger(__name__)

SOURCE = "open_library"
SEARCH_URL = "https://openlibrary.org/search.json"
FIELDS = (
    "key,title,author_name,ratings_average,ratings_count,edition_count,"
    "first_publish_year,publisher,want_to_read_count"
)


async def fetch_by_isbn(client: PoliteClient, isbn: str) -> RatingResult | None:
    doc = await _search(client, f"isbn:{isbn}")
    return _to_rating(doc) if doc else None


async def fetch_by_title_author(
    client: PoliteClient, title: str, author: str | None
) -> RatingResult | None:
    query = f'title:"{title}"'
    if author:
        query += f' author:"{author}"'
    doc = await _search(client, query)
    return _to_rating(doc) if doc else None


async def fetch_metadata(client: PoliteClient, isbn: str) -> dict[str, Any] | None:
    """Raw search doc, used for the in-print and popularity signals."""
    return await _search(client, f"isbn:{isbn}")


async def fetch_description(
    client: PoliteClient, *, isbn: str | None, title: str, author: str | None
) -> str | None:
    """Work-level description, used as a synopsis when Google Books has none.

    Worth the extra call: half the enriched books had no synopsis at all, and a
    blurb with nothing to ground it degrades into restating the rating and
    categories the digest card already shows. Open Library covers a useful
    share of those, and unlike Google Books it does not need an ISBN.
    """
    candidates: list[dict[str, Any]] = []

    # The ISBN gives the precise work, but Open Library holds several work
    # records per book and the one an ISBN resolves to is often a sparse stub
    # with no description at all. Free text finds the better-populated record.
    if isbn and (doc := await _search(client, f"isbn:{isbn}")):
        candidates.append(doc)

    loose = await _search(client, " ".join(filter(None, [title, author])))
    if loose and loose.get("key") not in {c.get("key") for c in candidates}:
        # Free text is imprecise, so confirm it is the same book before taking
        # prose from it — a wrong description is worse than none.
        verdict = verify(
            query_title=title,
            query_author=author,
            candidate_title=loose.get("title"),
            candidate_author=(loose.get("author_name") or [None])[0],
        )
        if verdict.accepted:
            candidates.append(loose)

    for doc in candidates:
        if not doc.get("key"):
            continue
        response = await client.get(f"https://openlibrary.org{doc['key']}.json")
        if response is None:
            continue
        try:
            description = response.json().get("description")
        except ValueError:
            continue

        # Sometimes a bare string, sometimes {"type": .., "value": ..}.
        if isinstance(description, dict):
            description = description.get("value")
        if isinstance(description, str) and description.strip():
            return description.strip()

    return None


async def fetch_want_to_read(client: PoliteClient, work_key: str) -> int | None:
    """Shelf counts: a popularity signal that exists even where ratings do not."""
    key = work_key.strip("/")
    response = await client.get(f"https://openlibrary.org/{key}/bookshelves.json")
    if response is None:
        return None
    try:
        return int(response.json()["counts"]["want_to_read"])
    except (KeyError, TypeError, ValueError):
        return None


async def _search(client: PoliteClient, query: str) -> dict[str, Any] | None:
    response = await client.get(
        SEARCH_URL, params={"q": query, "fields": FIELDS, "limit": 1}
    )
    if response is None:
        return None
    try:
        docs = response.json().get("docs") or []
    except ValueError:
        return None
    return docs[0] if docs else None


def _to_rating(doc: dict[str, Any]) -> RatingResult | None:
    rating = doc.get("ratings_average")
    count = doc.get("ratings_count")
    if rating is None or not count:
        return None
    authors = doc.get("author_name") or []
    return RatingResult(
        source=SOURCE,
        rating=float(rating),
        ratings_count=int(count),
        title=doc.get("title"),
        author=authors[0] if authors else None,
        url=f"https://openlibrary.org{doc['key']}" if doc.get("key") else None,
    )
