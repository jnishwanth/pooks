"""The rating fallback chain.

Sources are tried in the order given by config `[ratings].chain`. The first one
whose result clears `min_ratings_count` wins; every attempt is recorded in the
provenance dict so the dashboard can show where a number came from and why the
others were skipped.

The threshold is the important part. Without it Open Library's 2.33-from-3
would beat Goodreads' 4.11-from-7,516 simply by being asked first.
"""

from __future__ import annotations

import logging
from typing import Any

from pooks.enrich import goodreads, googlebooks, hardcover, openlibrary
from pooks.enrich.goodreads import NotRedirectedError
from pooks.enrich.http import PoliteClient
from pooks.enrich.match import MatchMethod, verify
from pooks.enrich.searxng import SearxngClient
from pooks.enrich.sources import RatingResult

log = logging.getLogger(__name__)


SOURCE_HOSTS = {
    "goodreads": "www.goodreads.com",
    "hardcover": "api.hardcover.app",
    "google_books": "www.googleapis.com",
    "open_library": "openlibrary.org",
}


def _host_for(source_name: str) -> str:
    return SOURCE_HOSTS.get(source_name, "")


class RatingResolver:
    def __init__(
        self,
        *,
        chain: list[str],
        min_ratings_count: int,
        searxng: SearxngClient,
        min_count_by_source: dict[str, int] | None = None,
        hardcover_key: str | None = None,
        google_books_key: str | None = None,
        accept_score: float = 92.0,
        reject_score: float = 70.0,
    ) -> None:
        self.chain = chain
        self.min_ratings_count = min_ratings_count
        self.min_count_by_source = min_count_by_source or {}
        self.searxng = searxng
        self.hardcover_key = hardcover_key
        self.google_books_key = google_books_key
        self.accept_score = accept_score
        self.reject_score = reject_score

    def floor_for(self, source_name: str) -> int:
        """Minimum rating count for a source, defaulting to the global floor."""
        return self.min_count_by_source.get(source_name, self.min_ratings_count)

    async def resolve(
        self,
        client: PoliteClient,
        *,
        isbn: str | None,
        title: str,
        author: str | None,
    ) -> tuple[RatingResult | None, dict[str, Any]]:
        provenance: dict[str, Any] = {
            "chain": list(self.chain),
            "min_counts": {s: self.floor_for(s) for s in self.chain},
            "attempts": {},
            "match_method": (MatchMethod.ISBN if isbn else MatchMethod.FUZZY).value,
        }
        ambiguous: list[str] = []
        degraded_sources: list[str] = []

        for source_name in self.chain:
            try:
                result = await self._fetch(client, source_name, isbn, title, author)
            except NotRedirectedError:
                # Ambiguous: possibly throttled, possibly a real miss. Recorded
                # as degraded so the result is cached for 30 minutes rather than
                # 30 days — the cost of guessing wrong is a month of a book
                # being wrongly marked unrated.
                provenance["attempts"][source_name] = {"result": "ambiguous (no redirect)"}
                degraded_sources.append(source_name)
                continue
            except Exception as exc:  # noqa: BLE001 - a broken source must not break enrichment
                log.warning("rating source %s raised: %s", source_name, exc)
                provenance["attempts"][source_name] = {"error": str(exc)}
                degraded_sources.append(source_name)
                continue

            if result is None:
                # "no match" and "we were blocked" look identical at this level
                # but mean opposite things — one is a fact about the book, the
                # other is a fact about our network. Conflating them makes a
                # throttling episode look like a catalogue gap.
                blocked = _host_for(source_name) in set(client.degraded_hosts())
                provenance["attempts"][source_name] = {
                    "result": "blocked" if blocked else "no match"
                }
                continue

            # A lookup that did not go through an ISBN needs verifying: it can
            # silently return a different book.
            if not isbn:
                verdict = verify(
                    query_title=title,
                    query_author=author,
                    candidate_title=result.title,
                    candidate_author=result.author,
                    accept_score=self.accept_score,
                    reject_score=self.reject_score,
                )
                if not verdict.accepted:
                    provenance["attempts"][source_name] = {
                        "result": "rejected by match check",
                        "score": verdict.score,
                        "candidate": result.title,
                    }
                    if verdict.ambiguous:
                        ambiguous.append(source_name)
                    continue
                provenance["attempts"][source_name] = {"match_score": verdict.score}

            attempt = provenance["attempts"].setdefault(source_name, {})
            attempt.update({"rating": result.rating, "ratings_count": result.ratings_count})

            floor = self.floor_for(source_name)
            if not result.is_usable(floor):
                attempt["result"] = (
                    f"below min_ratings_count ({result.ratings_count} < {floor})"
                )
                continue

            attempt["result"] = "accepted"
            provenance["accepted_source"] = source_name
            return result, provenance

        if ambiguous:
            # Left unresolved on purpose: a wrong match attaches another book's
            # rating and blurb, which is worse than having neither.
            provenance["ambiguous_matches"] = ambiguous
        degraded = client.degraded_hosts() + [_host_for(s) for s in degraded_sources]
        if degraded := [h for h in degraded if h]:
            # No rating found, but at least one source never really answered.
            # Recording this is what stops the miss being cached as permanent.
            provenance["degraded_hosts"] = sorted(set(degraded))
        return None, provenance

    async def _fetch(
        self,
        client: PoliteClient,
        source_name: str,
        isbn: str | None,
        title: str,
        author: str | None,
    ) -> RatingResult | None:
        if source_name == "goodreads":
            if isbn:
                return await goodreads.fetch_by_isbn(client, isbn)
            # No ISBN: SearXNG locates the book page the ISBN redirect can't.
            if self.searxng.available:
                if url := await self.searxng.find_goodreads_url(client, title, author):
                    return await goodreads.fetch_by_url(client, url)
            return None

        if source_name == "hardcover":
            return await hardcover.fetch_by_isbn(client, isbn, self.hardcover_key) if isbn else None

        if source_name == "google_books":
            if isbn:
                return await googlebooks.fetch_by_isbn(client, isbn, self.google_books_key)
            return await googlebooks.fetch_by_title_author(
                client, title, author, self.google_books_key
            )

        if source_name == "open_library":
            if isbn:
                return await openlibrary.fetch_by_isbn(client, isbn)
            return await openlibrary.fetch_by_title_author(client, title, author)

        log.warning("unknown rating source in chain: %s", source_name)
        return None
