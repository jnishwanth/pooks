"""Every answer every source gave, and the rule for choosing between them.

Enrichment used to keep one merged record per book and discard the losing
answers, which made three things impossible: seeing *why* a book has the rating
it has, telling "the primary source said nothing" apart from "the primary source
was never asked", and improving one field without risking another. The merge
that stood in for the third — keep the better of old and new, per field — was
hand-written, and being hand-written it had to be got right once per field.

An observation is one source's answer about one field of one book. Re-asking a
source replaces *that source's* row and nothing else, so Goodreads can never
clobber Hardcover. The record shown to the rest of the pipeline is then a pure
function of the rows: walk the configured ladder, take the first source that
answered usably. Monotonic by construction, because adding a row to a set can
only ever move the winner up the ladder.

Recording costs no extra requests. Sources are still tried in order and the
chain still short-circuits — this only stops the answers it already obtained
being thrown away. The repair pass fills the rest in over time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pooks.enrich.quality import price_tier
from pooks.enrich.sources import IndianPrice, RatingResult, ScarcitySignal, round_rating


class Field(StrEnum):
    """What an observation is about. Stored, so these are data rather than names."""

    RATING = "rating"
    SYNOPSIS = "synopsis"
    INDIAN_PRICE = "indian_price"
    TAGS = "tags"
    SCARCITY = "scarcity"


@dataclass(frozen=True)
class Observation:
    """One source's answer about one field, as stored."""

    field: Field
    source: str
    value: dict[str, Any]

    def encode(self) -> str:
        return json.dumps(self.value, default=str)


def rating_observation(result: RatingResult) -> Observation:
    return Observation(
        Field.RATING,
        result.source,
        {
            "rating": result.rating,
            "ratings_count": result.ratings_count,
            "title": result.title,
            "author": result.author,
        },
    )


def synopsis_observation(source: str, text: str) -> Observation:
    return Observation(Field.SYNOPSIS, source, {"synopsis": text})


def tags_observation(source: str, tags: dict[str, list[str]]) -> Observation:
    """A `{}` answer is recorded, not skipped.

    "Hardcover was asked and has nothing" is a settled fact for roughly two
    books in five, and the whole point of a stored observation is that it
    survives to say so — otherwise the repair pass asks again forever.
    """
    return Observation(Field.TAGS, source, {"tags": tags})


def price_observation(source: str, price: IndianPrice) -> Observation:
    return Observation(
        Field.INDIAN_PRICE,
        source,
        {
            "price_paise": price.price_paise,
            "available_in_india": price.available_in_india,
            "unknown": price.unknown,
        },
    )


def scarcity_observation(source: str, signal: ScarcitySignal) -> Observation:
    return Observation(
        Field.SCARCITY,
        source,
        {"listing_count": signal.listing_count, "has_new_offers": signal.has_new_offers},
    )


def _tier(source: str) -> int:
    """Price tier, with an unrecognised source sorting last rather than first.

    Written out because the obvious `price_tier(s) or 99` is a bug: amazon.in
    is tier 0 — the best — and zero is falsy, so the best source would sort
    behind every other.
    """
    tier = price_tier(source)
    return 99 if tier is None else tier


@dataclass(frozen=True)
class Ledger:
    """The observations held for one book, indexed for the projection below."""

    by_field: dict[Field, dict[str, dict[str, Any]]]

    @classmethod
    def from_rows(cls, rows: list[Any]) -> Ledger:
        by_field: dict[Field, dict[str, dict[str, Any]]] = {}
        for row in rows:
            try:
                field = Field(row["field"])
            except ValueError:
                # A field this version does not know about. Left in the table —
                # a downgrade must not silently delete a newer version's work.
                continue
            try:
                value = json.loads(row["value_json"])
            except ValueError:
                continue
            if isinstance(value, dict):
                by_field.setdefault(field, {})[row["source"]] = value
        return cls(by_field)

    def sources(self, field: Field) -> set[str]:
        """Which sources have answered about this field."""
        return set(self.by_field.get(field, {}))

    def _first(self, field: Field, ladder: list[str]) -> tuple[str, dict[str, Any]] | None:
        answers = self.by_field.get(field, {})
        for source in ladder:
            if source in answers:
                return source, answers[source]
        return None

    def rating(self, chain: list[str], floors: dict[str, int]) -> tuple[str, dict[str, Any]] | None:
        """The best usable rating, by the configured chain order.

        A source that answered with too thin a sample is skipped rather than
        accepted, exactly as the live chain does — the floor is per-source
        because what counts as thin depends on how big the community is.
        """
        answers = self.by_field.get(Field.RATING, {})
        for source in chain:
            answer = answers.get(source)
            if answer is None:
                continue
            count = answer.get("ratings_count") or 0
            rating = answer.get("rating") or 0
            if rating > 0 and count >= floors.get(source, 0):
                return source, answer
        return None

    def synopsis(self, chain: list[str]) -> str | None:
        """Longest wins among the sources that have one.

        Unlike a rating there is no quality ladder for prose, and the failure
        this replaces was a one-line stub from a sparse Open Library work record
        displacing a real publisher synopsis.
        """
        answers = self.by_field.get(Field.SYNOPSIS, {})
        texts = [str(a.get("synopsis") or "") for a in answers.values()]
        best = max(texts, key=len, default="")
        return best or None

    def indian_price(self) -> tuple[str, dict[str, Any]] | None:
        """Cheapest tier that found a real price, else any answer at all.

        Falling back to *any* answer matters: "no Indian retailer stocks this"
        and "every lookup was blocked" are both meaningful, and they are the two
        outcomes that must never be conflated.
        """
        answers = self.by_field.get(Field.INDIAN_PRICE, {})
        priced = {s: a for s, a in answers.items() if (a.get("price_paise") or 0) > 0}
        pool = priced or answers
        if not pool:
            return None
        # `or` is wrong here: amazon.in is tier *0*, the best one, and falsy.
        source = min(pool, key=lambda s: (_tier(s), s))
        return source, pool[source]

    def tags(self) -> dict[str, list[str]] | None:
        """None when nothing has answered — which is not the same as `{}`."""
        answers = self.by_field.get(Field.TAGS, {})
        for value in answers.values():
            tags = value.get("tags")
            if isinstance(tags, dict) and tags:
                return {k: list(v) for k, v in tags.items()}
        return {} if answers else None

    def scarcity(self) -> ScarcitySignal | None:
        for value in self.by_field.get(Field.SCARCITY, {}).values():
            return ScarcitySignal(
                listing_count=int(value.get("listing_count") or 0),
                has_new_offers=bool(value.get("has_new_offers")),
            )
        return None

    def resolved(self, chain: list[str], floors: dict[str, int]) -> tuple[str | None, str | None]:
        """Title and author as the winning rating source spelled them."""
        best = self.rating(chain, floors)
        if best is None:
            return None, None
        _, answer = best
        return answer.get("title"), answer.get("author")

    def price(self) -> IndianPrice | None:
        chosen = self.indian_price()
        if chosen is None:
            return None
        source, answer = chosen
        return IndianPrice(
            price_paise=answer.get("price_paise"),
            source=source,
            available_in_india=bool(answer.get("available_in_india")),
            unknown=bool(answer.get("unknown")),
        )

    def rating_value(
        self, chain: list[str], floors: dict[str, int]
    ) -> tuple[float, int, str] | None:
        best = self.rating(chain, floors)
        if best is None:
            return None
        source, answer = best
        return round_rating(float(answer["rating"])), int(answer["ratings_count"]), source
