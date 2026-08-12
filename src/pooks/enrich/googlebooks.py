"""Google Books.

Ratings are sparse and often thin, so this rarely wins the rating chain. It
earns its place as the synopsis source: the blurb generator needs retrieved
text to work from rather than model memory, and `description` is the most
consistently available synopsis of any free source.

An API key is effectively required — the shared anonymous quota is permanently
exhausted (verified: HTTP 429 "Quota exceeded ... Queries per day").
"""

from __future__ import annotations

import logging
from typing import Any

from pooks.enrich.http import PoliteClient
from pooks.enrich.sources import RatingResult

log = logging.getLogger(__name__)

SOURCE = "google_books"
VOLUMES_URL = "https://www.googleapis.com/books/v1/volumes"


async def fetch_by_isbn(
    client: PoliteClient, isbn: str, api_key: str | None = None
) -> RatingResult | None:
    volume = await _first_volume(client, f"isbn:{isbn}", api_key)
    return _to_rating(volume) if volume else None


async def fetch_by_title_author(
    client: PoliteClient, title: str, author: str | None, api_key: str | None = None
) -> RatingResult | None:
    query = f'intitle:"{title}"'
    if author:
        query += f' inauthor:"{author}"'
    volume = await _first_volume(client, query, api_key)
    return _to_rating(volume) if volume else None


async def fetch_volume_info(
    client: PoliteClient, isbn: str, api_key: str | None = None
) -> dict[str, Any] | None:
    """Full volumeInfo/saleInfo, used for synopsis and the in-print signal."""
    return await _first_volume(client, f"isbn:{isbn}", api_key)


async def _first_volume(
    client: PoliteClient, query: str, api_key: str | None
) -> dict[str, Any] | None:
    params: dict[str, Any] = {"q": query, "maxResults": 1, "country": "IN"}
    if api_key:
        params["key"] = api_key

    response = await client.get(VOLUMES_URL, params=params)
    if response is None:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None

    if "error" in payload:
        log.warning("google books error: %s", payload["error"].get("message"))
        return None
    items = payload.get("items") or []
    return items[0] if items else None


def _to_rating(volume: dict[str, Any]) -> RatingResult | None:
    info = volume.get("volumeInfo") or {}
    rating = info.get("averageRating")
    count = info.get("ratingsCount")
    if rating is None or not count:
        return None
    authors = info.get("authors") or []
    return RatingResult(
        source=SOURCE,
        rating=float(rating),
        ratings_count=int(count),
        title=info.get("title"),
        author=authors[0] if authors else None,
        synopsis=info.get("description"),
    )
