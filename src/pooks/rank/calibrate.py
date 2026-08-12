"""Turn the push thresholds from a guess into a measured choice.

`push_score_threshold` and `push_min_confidence` were picked before any book had
been scored. This reports the actual distribution and answers the only question
that matters: how many notifications would this setting have produced, and which
books would they have been?

Purely a read over stored scores — no network, no inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

from pooks.db.store import Store


@dataclass
class Calibration:
    """The score distribution over the in-stock catalogue.

    `books` pairs each score with its confidence because every question asked of
    a calibration needs both: a percentile is drawn from the books a threshold
    could actually push, and so is the count of them.
    """

    in_stock: int
    books: list[tuple[float, float]]
    suggestions: dict[str, float]

    @property
    def scored(self) -> int:
        return len(self.books)

    @property
    def scores(self) -> list[float]:
        return [score for score, _ in self.books]

    @property
    def confidences(self) -> list[float]:
        return [confidence for _, confidence in self.books]

    @property
    def enough_data(self) -> bool:
        """Below this, percentiles are noise rather than a distribution."""
        return self.scored >= 25

    def would_push(self, score_threshold: float, min_confidence: float) -> int:
        """How many books a setting would push.

        Exactly the set `would_notify` selects, counted from the distribution
        already in hand: reporting the current setting plus three candidate
        thresholds otherwise re-ran that query four times per invocation.
        """
        return sum(
            1
            for score, confidence in self.books
            if score >= score_threshold and confidence >= min_confidence
        )


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def would_notify(
    store: Store, score_threshold: float, min_confidence: float
) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        """
        SELECT p.name, p.price_paise, s.score, s.confidence
        FROM products p JOIN scores s ON s.product_id = p.product_id
        WHERE p.in_stock = 1 AND s.score >= ? AND COALESCE(s.confidence, 0) >= ?
        ORDER BY s.score DESC
        """,
        (score_threshold, min_confidence),
    ).fetchall()
    return [
        {
            "name": row["name"],
            "price_inr": (row["price_paise"] or 0) / 100,
            "score": row["score"],
            "confidence": row["confidence"] or 0.0,
        }
        for row in rows
    ]


def calibrate(store: Store, min_confidence: float = 0.5) -> Calibration:
    # LEFT JOIN, so an in-stock book with no score still counts toward the
    # denominator. An inner join made "scored / in stock" read `8 / 8` on a
    # catalogue of 633 — true of the rows it selected, and useless.
    rows = store.conn.execute(
        """
        SELECT s.score, s.confidence FROM products p
        LEFT JOIN scores s ON s.product_id = p.product_id
        WHERE p.in_stock = 1
        """
    ).fetchall()

    books = [
        (r["score"], r["confidence"] or 0.0) for r in rows if r["score"] is not None
    ]

    # Only books that could actually be pushed inform the score threshold —
    # low-confidence ones are gated out regardless of where it sits.
    eligible = [score for score, confidence in books if confidence >= min_confidence]

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

    out.append(f"score  median {median(calibration.scores):.3f}  "
               f"min {min(calibration.scores):.3f}  max {max(calibration.scores):.3f}")
    out.append(f"conf   median {median(calibration.confidences):.3f}")
    out.append("\nscore distribution:")
    out.extend(histogram(calibration.scores))

    out.append(
        f"\ncurrent settings (score >= {current_threshold}, conf >= "
        f"{current_confidence}) would push "
        f"{calibration.would_push(current_threshold, current_confidence)} of "
        f"{calibration.scored} scored books"
    )

    if calibration.suggestions:
        out.append("\nthresholds by share of eligible books:")
        for label, value in calibration.suggestions.items():
            count = calibration.would_push(value, current_confidence)
            out.append(f"  {label:<12} score >= {value:.3f}   -> {count} book(s)")

    if not calibration.enough_data:
        out.append(
            f"\nCAVEAT: only {calibration.scored} books scored. These percentiles "
            "are noise until the catalogue is backfilled — treat them as a "
            "sanity check, not a recommendation."
        )
    return out
