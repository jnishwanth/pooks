"""Self-hosted SearXNG, used as a resolver rather than a data source.

Search snippets turned out to be a poor place to read ratings from: a Goodreads
result's snippet is built from `og:description`, which is the book blurb, not
the rating. What SearXNG is genuinely good at is finding the right *URL* — which
is exactly what the ~7% of listings without an ISBN need, since Goodreads'
ISBN redirect cannot help them.

Requires `search.formats: [html, json]` in the instance's settings.yml.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from pooks.enrich.http import PoliteClient

log = logging.getLogger(__name__)

GOODREADS_BOOK = re.compile(r"^https?://(?:www\.)?goodreads\.com/(?:[a-z]{2}/)?book/show/\d+")


@dataclass
class SearchHit:
    title: str
    url: str
    content: str


class SearxngClient:
    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    async def search(self, client: PoliteClient, query: str, *, limit: int = 10) -> list[SearchHit]:
        if not self.base_url:
            return []

        response = await client.get(
            f"{self.base_url}/search",
            params={"q": query, "format": "json", "language": "en"},
        )
        if response is None:
            return []

        try:
            results = response.json().get("results") or []
        except ValueError:
            log.warning(
                "searxng did not return JSON. Enable 'json' in search.formats in settings.yml."
            )
            return []

        return [
            SearchHit(
                title=item.get("title") or "",
                url=item.get("url") or "",
                content=item.get("content") or "",
            )
            for item in results[:limit]
            if item.get("url")
        ]

    async def find_goodreads_url(
        self, client: PoliteClient, title: str, author: str | None
    ) -> str | None:
        """Locate a Goodreads book page for a title that has no usable ISBN."""
        query = f"{title} {author or ''} goodreads".strip()
        for hit in await self.search(client, query):
            if GOODREADS_BOOK.match(hit.url):
                return hit.url
        return None
