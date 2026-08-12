"""Goodreads ratings via published schema.org data.

Goodreads retired its public API and issues no new keys, but every book page
embeds a schema.org `Book` block with an `aggregateRating`. Two properties make
this the strongest source available:

  * /search?q=<isbn> redirects straight to the canonical book page, so an ISBN
    resolves in a single request;
  * the aggregate is work-level, so it does not suffer the edition
    fragmentation that makes per-edition numbers unusable (the same title can
    show 3.80/213, 3.81/32 and 4.00/6 across its editions).

Volume is roughly 15 lookups a day, each cached by ISBN permanently, paced by
PoliteClient to one request per 5 seconds.
"""

from __future__ import annotations

import logging

from pooks.enrich.http import PoliteClient
from pooks.enrich.jsonld import as_float, as_int, extract_blocks, find_by_type
from pooks.enrich.sources import RatingResult

log = logging.getLogger(__name__)

SOURCE = "goodreads"
SEARCH_URL = "https://www.goodreads.com/search"


class NotRedirectedError(Exception):
    """Goodreads served the search page instead of redirecting to a book.

    A third failure mode, distinct from the 202-empty soft block. Under load the
    ISBN redirect stops firing and a plain search page comes back, which is
    indistinguishable from "this ISBN is unknown" — except that the same ISBN
    redirects fine minutes later. Observed directly: three ISBNs resolved to
    ratings on one run and "no match" on the next.

    It matters because of caching. A genuine miss is cached for 30 days; if a
    throttle is mistaken for one, the book is marked unrated for a month on the
    strength of a temporary rate limit.
    """


async def fetch_by_isbn(client: PoliteClient, isbn: str) -> RatingResult | None:
    response = await client.get(SEARCH_URL, params={"q": isbn})
    if response is None:
        return None
    if "/book/show/" not in str(response.url):
        log.info(
            "goodreads: isbn %s did not redirect to a book page — treating as "
            "possibly throttled rather than a confirmed miss",
            isbn,
        )
        raise NotRedirectedError(isbn)
    return _parse(response.text)


async def fetch_by_url(client: PoliteClient, url: str) -> RatingResult | None:
    response = await client.get(url)
    if response is None:
        return None
    return _parse(response.text)


def _parse(html: str) -> RatingResult | None:
    blocks = extract_blocks(html)
    for book in find_by_type(blocks, "Book"):
        aggregate = book.get("aggregateRating") or {}
        rating = as_float(aggregate.get("ratingValue"))
        count = as_int(aggregate.get("ratingCount"))
        if rating is None or count is None:
            continue

        author = book.get("author")
        if isinstance(author, list):
            author = author[0] if author else None
        if isinstance(author, dict):
            author = author.get("name")

        return RatingResult(
            source=SOURCE,
            rating=rating,
            ratings_count=count,
            title=book.get("name"),
            author=author if isinstance(author, str) else None,
        )
    return None
