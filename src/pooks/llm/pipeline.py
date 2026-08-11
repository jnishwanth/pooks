"""Cached invocation of the LLM roles.

Every result is cached by (book_key, role, prompt_version). Because book_key is
ISBN-derived, a title the shop relists costs nothing the second time — which is
what keeps a catalogue of ~30,000 recurring listings inside a free tier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pooks.db.store import Store, transaction
from pooks.enrich.sources import BookFacts
from pooks.llm.client import LLMClient, LLMUnavailableError
from pooks.llm.roles import (
    Blurb,
    Renown,
    RenownTier,
    Role,
    generate_blurb,
    judge_renown,
)
from pooks.models import Product

log = logging.getLogger(__name__)


@dataclass
class BookInsights:
    blurb: str | None = None
    insufficient_context: bool = False
    spoiler_flagged: bool = False
    renown_tier: str = RenownTier.UNKNOWN.value
    renown_score: float | None = None
    renown_abstained: bool = True
    renown_evidence: str = ""
    from_cache: bool = False
    skipped_reason: str | None = None


class InsightGenerator:
    def __init__(self, client: LLMClient, prompt_version: int) -> None:
        self.client = client
        self.prompt_version = prompt_version

    async def generate(
        self,
        store: Store,
        product: Product,
        facts: BookFacts,
        *,
        force: bool = False,
    ) -> BookInsights:
        book_key = product.book_key

        if not self.client.available:
            return BookInsights(
                skipped_reason=self.client.credential_problem()
                or "no LLM provider configured"
            )

        cached_blurb = None if force else store.get_llm(book_key, Role.BLURB, self.prompt_version)
        cached_renown = None if force else store.get_llm(book_key, Role.RENOWN, self.prompt_version)

        if cached_blurb is not None and cached_renown is not None:
            return insights_from_cache(cached_blurb, cached_renown)

        insights = BookInsights()

        blurb_payload = cached_blurb
        if blurb_payload is None and not (facts.synopsis or "").strip():
            # No retrieved text to work from. The design is explicit that blurbs
            # are grounded rather than recalled, and without a synopsis the model
            # pads with metadata the digest card already shows ("categorized as
            # history and non-fiction. With a 3.77/5 rating from 337 readers").
            # Skipping is both better output and a saved call.
            log.info("no synopsis for %s; skipping the blurb", book_key)
            blurb_payload = {
                "blurb": "",
                "insufficient_context": True,
                "spoiler_flagged": False,
                "reason": "no synopsis available to ground it",
            }

        elif blurb_payload is None:
            try:
                blurb, verdict = await generate_blurb(
                    self.client,
                    title=facts.resolved_title or product.work_title,
                    author=facts.resolved_author or product.author,
                    synopsis=facts.synopsis,
                    categories=product.categories,
                    rating=facts.rating,
                    ratings_count=facts.ratings_count,
                )
            except LLMUnavailableError as exc:
                log.warning("blurb generation failed for %s: %s", book_key, exc)
                blurb, verdict = Blurb(blurb="", insufficient_context=True), None

            blurb_payload = {
                "blurb": blurb.blurb,
                "insufficient_context": blurb.insufficient_context,
                "spoiler_flagged": bool(verdict and verdict.has_spoilers),
            }
            # Never cache a failure. A rate-limited call returns empty text, and
            # caching that pinned the book to a blank blurb permanently — the
            # only escape being a prompt_version bump, which discards every
            # role for every book. Eight books were already stuck this way.
            if blurb.blurb.strip():
                _store(
                    store,
                    book_key,
                    Role.BLURB,
                    self.prompt_version,
                    blurb_payload,
                    self.client.model,
                )
            else:
                log.info("not caching an empty blurb for %s; it will be retried", book_key)

        renown_payload = cached_renown
        if renown_payload is None:
            renown = await judge_renown(
                self.client,
                title=facts.resolved_title or product.work_title,
                author=facts.resolved_author or product.author,
                publisher=product.publisher,
                year=_year(facts),
                categories=product.categories,
                rating=facts.rating,
                ratings_count=facts.ratings_count,
            )
            renown_payload = renown.model_dump(mode="json")
            # A genuine abstention is a real answer and worth keeping; one caused
            # by an unreachable model is not.
            if not renown.unavailable:
                _store(
                    store,
                    book_key,
                    Role.RENOWN,
                    self.prompt_version,
                    renown_payload,
                    self.client.model,
                )

        insights = insights_from_cache(blurb_payload, renown_payload)
        insights.from_cache = False
        return insights


def insights_from_cache(blurb: dict[str, Any], renown: dict[str, Any]) -> BookInsights:
    parsed = Renown.model_validate(renown)
    return BookInsights(
        blurb=blurb.get("blurb") or None,
        insufficient_context=bool(blurb.get("insufficient_context")),
        spoiler_flagged=bool(blurb.get("spoiler_flagged")),
        renown_tier=parsed.tier.value,
        renown_score=parsed.score,
        renown_abstained=parsed.abstained,
        renown_evidence=parsed.evidence,
        from_cache=True,
    )


def _store(
    store: Store,
    book_key: str,
    role: str,
    version: int,
    payload: dict[str, Any],
    model: str | None,
) -> None:
    with transaction(store.conn):
        store.put_llm(book_key, role, version, payload, model)


def _year(facts: BookFacts) -> int | None:
    meta = facts.provenance.get("open_library_meta") or {}
    year = meta.get("first_publish_year")
    return int(year) if isinstance(year, int | str) and str(year).isdigit() else None
