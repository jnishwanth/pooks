"""Hardcover — a modern Goodreads alternative with a free GraphQL API.

The query below is verified working against the live API (ISBN 9780140020304
returns title, slug, rating, ratings_count, description and edition pages).

One trap: Hardcover's dashboard gives you the token with `Bearer ` already
prefixed, so naively formatting `Bearer {key}` produces `Bearer Bearer eyJ...`
and the API rejects it with "Malformed Authorization header". `_auth_header`
normalises both forms.

Everything still fails soft — a schema change logs and returns None, dropping
through to the next source rather than breaking enrichment.
"""

from __future__ import annotations

import logging
from typing import Any

from pooks.enrich.http import PoliteClient
from pooks.enrich.sources import RatingResult

log = logging.getLogger(__name__)

SOURCE = "hardcover"
GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"

ISBN_QUERY = """
query BookByIsbn($isbn: String!) {
  editions(where: {isbn_13: {_eq: $isbn}}, limit: 1) {
    pages
    book {
      title
      slug
      rating
      ratings_count
      description
      contributions { author { name } }
    }
  }
}
"""


def _auth_header(api_key: str) -> str:
    """Build the Authorization value, tolerating a key that already has the scheme.

    Hardcover's dashboard hands you the token with `Bearer ` already prefixed.
    Prepending it again yields `Bearer Bearer eyJ...`, which the API rejects with
    "Malformed Authorization header" — observed in practice.
    """
    token = api_key.strip()
    if token.lower().startswith("bearer "):
        return f"Bearer {token[7:].strip()}"
    return f"Bearer {token}"


async def fetch_by_isbn(
    client: PoliteClient, isbn: str, api_key: str | None
) -> RatingResult | None:
    if not api_key:
        return None

    response = await client.post_json(
        GRAPHQL_URL,
        {"query": ISBN_QUERY, "variables": {"isbn": isbn}},
        headers={"Authorization": _auth_header(api_key)},
    )
    if response is None:
        return None

    try:
        payload = response.json()
    except ValueError:
        return None

    if errors := payload.get("errors"):
        log.warning(
            "hardcover query rejected (%s). The schema in this module is "
            "unvalidated; check field names against the current API.",
            errors[0].get("message") if errors else "unknown",
        )
        return None

    editions = (payload.get("data") or {}).get("editions") or []
    if not editions:
        return None
    return _to_rating(editions[0])


def _to_rating(edition: dict[str, Any]) -> RatingResult | None:
    book = edition.get("book") or {}
    rating = book.get("rating")
    count = book.get("ratings_count")
    if rating is None or not count:
        return None

    author = None
    for contribution in book.get("contributions") or []:
        if name := (contribution.get("author") or {}).get("name"):
            author = name
            break

    slug = book.get("slug")
    return RatingResult(
        source=SOURCE,
        rating=float(rating),
        ratings_count=int(count),
        title=book.get("title"),
        author=author,
        url=f"https://hardcover.app/books/{slug}" if slug else None,
        synopsis=book.get("description"),
        pages=edition.get("pages"),
    )
