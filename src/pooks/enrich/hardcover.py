"""Hardcover — a modern Goodreads alternative with a free GraphQL API.

The query below is verified working against the live API (ISBN 9780140020304
returns title, rating, ratings_count, description and tags). It asks only for
what is read: an edition's page count and the book's slug were requested and
then discarded.

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
    book {
      title
      rating
      ratings_count
      description
      cached_tags
      contributions { author { name } }
    }
  }
}
"""

# Hardcover groups tags under these headings. They are kept as its own slugs so
# a filter built on them stays stable, rather than being renamed into a
# vocabulary of our own invention.
TAG_FACETS: dict[str, str] = {
    "Genre": "genre",
    "Mood": "mood",
    "Tag": "tags",
    "Content Warning": "content_warning",
}


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


async def fetch_tags(
    client: PoliteClient, isbn: str, api_key: str | None
) -> dict[str, list[str]] | None:
    """Genre/mood/reader tags, or None if we never got an answer.

    The distinction matters: `{}` means Hardcover was asked and has nothing,
    which is a settled fact for roughly two books in five, while None means the
    question was never put. Conflating them would mark those books improvable
    forever and burn the repair budget on a lookup that can never succeed.

    Called unconditionally rather than as part of the rating chain. Hardcover
    sits second there, so whenever Goodreads answers — the common case — it is
    skipped entirely and no tags would ever arrive.
    """
    edition, answered = await _fetch_edition(client, isbn, api_key)
    if not answered:
        return None
    if edition is None:
        # Hardcover replied and has no such book. Settled, not a failure —
        # retrying it forever would be the same mistake as treating a blocked
        # price lookup as scarcity.
        return {}

    cached = (edition.get("book") or {}).get("cached_tags")
    if not isinstance(cached, dict):
        return {}

    tags: dict[str, list[str]] = {}
    for heading, facet in TAG_FACETS.items():
        entries = cached.get(heading) or []
        slugs = [
            e["tagSlug"]
            for e in entries
            if isinstance(e, dict) and e.get("tagSlug")
        ]
        if slugs:
            tags[facet] = slugs
    return tags


async def fetch_by_isbn(
    client: PoliteClient, isbn: str, api_key: str | None
) -> RatingResult | None:
    edition, _ = await _fetch_edition(client, isbn, api_key)
    return _to_rating(edition) if edition else None


async def _fetch_edition(
    client: PoliteClient, isbn: str, api_key: str | None
) -> tuple[dict[str, Any] | None, bool]:
    """Returns `(edition, answered)`.

    `answered` separates "Hardcover replied, and has nothing for this ISBN" —
    true for roughly two books in five and a settled fact — from "we never got a
    reply", which is transient and must be retried. Collapsing the two into a
    single None was enough to make an absent book look identical to a blocked
    request.

    One query serves both the rating and the tags. A book can cost two calls
    when Goodreads fails and the rating chain reaches Hardcover as well —
    acceptable at 1s pacing, and only for the minority where that happens.
    """
    if not api_key:
        return None, False

    response = await client.post_json(
        GRAPHQL_URL,
        {"query": ISBN_QUERY, "variables": {"isbn": isbn}},
        headers={"Authorization": _auth_header(api_key)},
    )
    if response is None:
        return None, False

    try:
        payload = response.json()
    except ValueError:
        return None, False

    if errors := payload.get("errors"):
        log.warning(
            "hardcover query rejected (%s) — check the field names in this "
            "module against the current API.",
            errors[0].get("message") if errors else "unknown",
        )
        return None, False

    editions = (payload.get("data") or {}).get("editions") or []
    return (editions[0] if editions else None), True


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

    return RatingResult(
        source=SOURCE,
        rating=float(rating),
        ratings_count=int(count),
        title=book.get("title"),
        author=author,
        synopsis=book.get("description"),
    )
