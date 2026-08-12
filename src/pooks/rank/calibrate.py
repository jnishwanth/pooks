"""Turn the push thresholds from a guess into a measured choice.

`push_score_threshold` and `push_min_confidence` were picked before any book had
been scored. This reports the actual distribution and answers the only question
that matters: how many notifications would this setting have produced, and which
books would they have been?

An answer here is only worth anything if it is the answer the pipeline would
give, so the gate itself comes from `rank.score.pushable` — the same predicate
`run.process_pending` applies when the push actually happens.

Purely a read over stored scores — no network, no inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import NamedTuple

from pooks.db.store import Store
from pooks.rank.score import pushable


class ScoredBook(NamedTuple):
    """One scored in-stock listing, as a calibration sees it."""

    name: str
    price_paise: int | None
    score: float
    confidence: float

    @property
    def price_inr(self) -> float:
        return (self.price_paise or 0) / 100


@dataclass
class Calibration:
    """The score distribution over the in-stock catalogue.

    `books` keeps each score with its confidence and its listing because every
    question asked of a calibration needs all three: a percentile is drawn from
    the books a threshold could actually push, so is the count of them, and so
    is the list `pooks calibrate` prints.
    """

    in_stock: int
    books: list[ScoredBook]
    suggestions: dict[str, float]

    @property
    def scored(self) -> int:
        return len(self.books)

    @property
    def scores(self) -> list[float]:
        return [book.score for book in self.books]

    @property
    def confidences(self) -> list[float]:
        return [book.confidence for book in self.books]

    @property
    def enough_data(self) -> bool:
        """Below this, percentiles are noise rather than a distribution."""
        return self.scored >= 25

    def would_push(self, score_threshold: float, min_confidence: float) -> list[ScoredBook]:
        """The books a setting would push, best first.

        Answered from the distribution already in hand rather than by a second
        query: the summary asks this once for the current setting and once per
        suggested threshold, and the CLI asks again for the listing.
        """
        return sorted(
            (
                book
                for book in self.books
                if pushable(
                    book.score,
                    book.confidence,
                    threshold=score_threshold,
                    min_confidence=min_confidence,
                )
            ),
            key=lambda book: book.score,
            reverse=True,
        )


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def calibrate(store: Store, min_confidence: float = 0.5) -> Calibration:
    # LEFT JOIN, so an in-stock book with no score still counts toward the
    # denominator. An inner join made "scored / in stock" read `8 / 8` on a
    # catalogue of 633 — true of the rows it selected, and useless.
    rows = store.conn.execute(
        """
        SELECT p.name, p.price_paise, s.score, s.confidence FROM products p
        LEFT JOIN scores s ON s.product_id = p.product_id
        WHERE p.in_stock = 1
        """
    ).fetchall()

    books = [
        ScoredBook(r["name"], r["price_paise"], r["score"], r["confidence"] or 0.0)
        for r in rows
        if r["score"] is not None
    ]

    # Only books that could actually be pushed inform the score threshold —
    # low-confidence ones are gated out regardless of where it sits.
    eligible = [book.score for book in books if book.confidence >= min_confidence]

    suggestions: dict[str, float] = {}
    if eligible:
        # Arrivals run ~10-15/day. These percentiles are expressed as the share
        # of *eligible* books that would push, so the daily volume follows.
        suggestions = {
            "top_10pct": percentile(eligible, 0.90),
            "top_25pct": percentile(eligible, 0.75),
            "top_50pct": percentile(eligible, 0.50),
        }

    return Calibration(in_stock=len(rows), books=books, suggestions=suggestions)


def histogram(values: list[float], buckets: int = 10, width: int = 34) -> list[str]:
    if not values:
        return ["  (no data)"]
    counts = [0] * buckets
    for value in values:
        counts[min(int(value * buckets), buckets - 1)] += 1
    peak = max(counts) or 1
    lines = []
    for index, count in enumerate(counts):
        low = index / buckets
        bar = "#" * int(round(width * count / peak))
        lines.append(f"  {low:.1f}-{low + 1 / buckets:.1f}  {bar:<{width}} {count}")
    return lines


def summarise(
    calibration: Calibration, current_threshold: float, current_confidence: float
) -> list[str]:
    out: list[str] = []
    out.append(f"in-stock books scored : {calibration.scored} / {calibration.in_stock}")

    if not calibration.scored:
        out.append("\nNothing scored yet — run 'pooks process' first.")
        return out

    out.append(
        f"score  median {median(calibration.scores):.3f}  "
        f"min {min(calibration.scores):.3f}  max {max(calibration.scores):.3f}"
    )
    out.append(f"conf   median {median(calibration.confidences):.3f}")
    out.append("\nscore distribution:")
    out.extend(histogram(calibration.scores))

    out.append(
        f"\ncurrent settings (score >= {current_threshold}, conf >= "
        f"{current_confidence}) would push "
        f"{len(calibration.would_push(current_threshold, current_confidence))} of "
        f"{calibration.scored} scored books"
    )

    if calibration.suggestions:
        out.append("\nthresholds by share of eligible books:")
        for label, value in calibration.suggestions.items():
            count = len(calibration.would_push(value, current_confidence))
            out.append(f"  {label:<12} score >= {value:.3f}   -> {count} book(s)")

    if not calibration.enough_data:
        out.append(
            f"\nCAVEAT: only {calibration.scored} books scored. These percentiles "
            "are noise until the catalogue is backfilled — treat them as a "
            "sanity check, not a recommendation."
        )
    return out
