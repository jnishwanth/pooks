"""Composite scoring.

Rating leads, renown second, value third — with every component kept alongside
the composite so a ranking can be explained rather than just asserted.

Two decisions carry most of the weight:

*Bayesian shrinkage.* Raw averages are unusable across a catalogue where rating
counts span three orders of magnitude. Recon repeatedly turned up thin ratings
that would otherwise dominate: 2.33 from 3 ratings, 4.00 from 6, 3.71 from 14.
Shrinking toward the global mean in proportion to sample size is what stops a
5.0-from-3 outranking a 4.2-from-20,000.

*Confidence.* A score computed from one rating and no comps is not comparable to
one backed by 19,000 ratings and 11 listings. Confidence is tracked separately
and gates notification, rather than being blended into the score where it would
be invisible.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from pooks.config import Config
from pooks.enrich.sources import BookFacts
from pooks.llm.pipeline import BookInsights
from pooks.models import Product

# Observed ratings cluster tightly between roughly 3.4 and 4.4. Normalising the
# full 1-5 scale would squash every book into a narrow band and destroy
# discrimination, so the band is stretched over the range that actually occurs.
#
# These stay absolute, with a per-category baseline applied as a *shift* rather
# than by rebuilding them around it. Deriving the floor as `baseline - 0.9`
# looks equivalent and is not: 3.9 - 0.9 is 3.0000000000000004 and 0.9 + 0.7 is
# 1.5999999999999999, so every score moved in its last bits even with no
# categories configured. A shift of exactly 0.0 leaves the arithmetic untouched,
# which is what makes "no baselines means no change" true rather than nearly.
QUALITY_FLOOR = 3.0
QUALITY_CEILING = 4.6

# Rating volume as a renown proxy when the model abstains, on a log scale:
# ~1M ratings saturates, ~1,000 lands mid-range.
RENOWN_LOG_SATURATION = 1_000_000

# Where a book with no supporting evidence lands: deliberately middling, so an
# unknown book neither tops nor bottoms the ranking on no information.
NEUTRAL_PRIOR = 0.45
# Confidence at which a score is trusted at face value. Above this, no shrinkage.
EVIDENCE_SATURATION = 0.60

RENOWN_TIER_SCORES = {
    "canonical": 1.0,
    "major": 0.8,
    "notable": 0.6,
    "standard": 0.35,
    "unknown": None,
}


@dataclass
class ScoreBreakdown:
    score: float
    quality: float | None
    renown: float | None
    value: float | None
    condition_factor: float
    confidence: float
    notes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_stored(cls, breakdown_json: str) -> ScoreBreakdown:
        """Rebuild a breakdown from the `scores.breakdown_json` column.

        The inverse of `as_dict`, and the faithful way to read a stored score
        back: the individual `scores` columns are denormalised copies kept for
        querying, so reassembling from them loses `notes` and invites a caller
        to invent the fields it did not select.

        Keys the dataclass no longer has are dropped rather than raising.
        `affordability` was a scoring component until it was retired, and every
        row written before then still carries it — a rescore rewrites the row,
        but a book that has since gone out of stock is never rescored.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in json.loads(breakdown_json).items() if k in known})


def pushable(score: float, confidence: float, *, threshold: float, min_confidence: float) -> bool:
    """Whether a scored book clears both push gates.

    The score half of the push decision — `models.notifiable` is the event half.
    It lives here, in floats rather than on `ScoreBreakdown`, because its second
    caller is `rank.calibrate`, which reads scores back out of the database and
    exists precisely to predict what `run.process_pending` will push. A second
    spelling of the rule there would make the prediction wrong rather than
    merely duplicated.
    """
    return score >= threshold and confidence >= min_confidence


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def bayesian_rating(rating: float, count: int, prior_count: int, global_mean: float) -> float:
    """Shrink a rating toward the global mean in proportion to sample size."""
    return (count * rating + prior_count * global_mean) / (count + prior_count)


@dataclass(frozen=True)
class Baseline:
    """The rating distribution a book is judged against.

    `shift` is the offset from the global mean, and is exactly 0.0 when no
    category baseline applied — which is what keeps the default path bit-for-bit
    identical to scoring with no categories at all.
    """

    mean: float
    category: str | None
    shift: float


def category_baseline(categories: list[str], config: Config) -> Baseline:
    """The mean rating this kind of book is judged against, and which kind.

    A rating only means something relative to its own category. Manga and
    children's books are rated by people already invested in the series, so
    their distributions sit structurally higher: on the live catalogue Naruto 30
    scores 4.4 from 8,574 against 4.13 from 19,195 for *Memoirs of a Dutiful
    Daughter*, and Bayesian shrinkage cannot separate them because both samples
    are large. Judged against one global mean, the manga wins on quality
    outright.

    Baselines are keyed on the shop's own categories, which cover the whole
    catalogue where Hardcover tags reach about three books in five, and they are
    *measured* — `pooks calibrate` reports the observed mean per category. With
    `[ranking.category_baselines]` empty, which is the shipped default, this
    returns the global mean and the scoring is unchanged.

    A book in several categories is judged against the highest baseline it
    belongs to: the most forgiving crowd it is a member of is the one whose
    ratings it benefits from.
    """
    global_mean = config.bayes_global_mean
    baselines = config.category_baselines
    matched = {c: baselines[c] for c in categories if c in baselines}
    if not matched:
        return Baseline(global_mean, None, 0.0)
    name = max(matched, key=lambda c: matched[c])
    return Baseline(matched[name], name, matched[name] - global_mean)


def quality_component(
    facts: BookFacts, prior_count: int, baseline: Baseline
) -> tuple[float | None, dict[str, Any]]:
    rated = facts.rating_with_count()
    if rated is None:
        return None, {"reason": "no usable rating found"}
    rating, count = rated

    # Both the prior and the band move with the baseline, so a thin sample
    # shrinks toward its own category's mean and a 4.4 is read against what 4.4
    # means for that kind of book.
    shrunk = bayesian_rating(rating, count, prior_count, baseline.mean)
    floor = QUALITY_FLOOR + baseline.shift
    normalised = clamp((shrunk - floor) / (QUALITY_CEILING - QUALITY_FLOOR))
    notes = {
        "raw_rating": rating,
        "ratings_count": count,
        "shrunk_rating": round(shrunk, 3),
        "source": facts.rating_source,
    }
    if baseline.category is not None:
        # Only recorded when it did something, so a breakdown from a catalogue
        # with no baselines configured reads exactly as it always has.
        notes["baseline"] = round(baseline.mean, 3)
        notes["baseline_from"] = baseline.category
    return normalised, notes


def renown_component(
    facts: BookFacts, insights: BookInsights
) -> tuple[float | None, dict[str, Any]]:
    """Model judgement where it is willing to commit, rating volume otherwise."""
    if not insights.renown_abstained and insights.renown_score is not None:
        return clamp(insights.renown_score), {
            "source": "llm",
            "tier": insights.renown_tier,
            "evidence": insights.renown_evidence,
        }

    tier_score = RENOWN_TIER_SCORES.get(insights.renown_tier)
    if tier_score is not None:
        return tier_score, {"source": "llm_tier", "tier": insights.renown_tier}

    if facts.ratings_count:
        proxy = clamp(math.log10(max(facts.ratings_count, 1)) / math.log10(RENOWN_LOG_SATURATION))
        return proxy, {
            "source": "ratings_volume_proxy",
            "ratings_count": facts.ratings_count,
            "note": "model abstained; using rating volume as a reach proxy",
        }

    want_to_read = facts.provenance.get("want_to_read")
    if want_to_read:
        proxy = clamp(math.log10(max(int(want_to_read), 1)) / math.log10(10_000))
        return proxy, {"source": "open_library_want_to_read", "want_to_read": want_to_read}

    return None, {"reason": "no renown signal available"}


def value_component(product: Product, facts: BookFacts) -> tuple[float | None, dict[str, Any]]:
    """How good the shop's price is against what you would otherwise pay.

    The baseline is the cheapest Indian price, because that is the real
    alternative for the buyer. It replaced an AbeBooks comparison that could not
    work: AbeBooks quotes USD, Indian prices sit structurally far below US/UK
    ones, and every book therefore came out 87-91% "cheaper" — a constant across
    mass-market paperbacks, manga and scarce literary memoirs alike.

    AbeBooks still contributes scarcity, which is currency-independent: a book
    with two copies worldwide is a different proposition from one with two
    hundred, whatever it costs.
    """
    price = product.price_paise
    indian = facts.indian_price
    signal = facts.scarcity
    scarcity = signal.scarcity if signal and signal.has_data else 0.0
    notes: dict[str, Any] = {"in_print": facts.in_print, "scarcity": round(scarcity, 3)}

    if price is None:
        return None, {"reason": "no shop price"}

    # Bound before the branch: `has_price` is a property, so it cannot narrow
    # `price_paise` for the division, and it is its `> 0` half that keeps that
    # division safe rather than merely "is set".
    baseline = indian.price_paise if indian and indian.has_price else None

    if indian is not None and baseline is not None:
        saving = clamp((baseline - price) / baseline)
        notes.update(
            {
                "basis": f"cheapest in india ({indian.source})",
                "shop_inr": price / 100,
                "india_inr": baseline / 100,
                "saving": round(saving, 3),
            }
        )
        return clamp(0.8 * saving + 0.2 * scarcity), notes

    # Order matters: a blocked lookup also leaves available_in_india False, and
    # scoring a network failure as scarcity would reward the failure.
    if indian and indian.unknown:
        notes.update({"basis": "india lookup blocked — unknown, not scarce"})
        return None, notes

    if indian and not indian.available_in_india:
        notes.update({"basis": "not sold in india — import only"})
        return 0.7, notes

    if facts.in_print is False:
        notes.update({"basis": "out of print, no indian price"})
        return 0.7, notes

    notes.update({"basis": "no reference price available"})
    return None, notes


def confidence(facts: BookFacts, insights: BookInsights) -> tuple[float, dict[str, Any]]:
    """How much real evidence the score rests on.

    Kept separate from the score so a thin-evidence book cannot quietly reach
    the top of a digest; the notifier gates on it.
    """
    total = 0.0
    parts: dict[str, Any] = {}

    rated = facts.rating_with_count()
    if rated is not None:
        _, count = rated
        volume = clamp(math.log10(max(count, 1)) / math.log10(50_000))
        contribution = 0.30 + 0.25 * volume
        total += contribution
        parts["rating"] = round(contribution, 3)

    if facts.indian_price and facts.indian_price.has_price:
        total += 0.20
        parts["indian_price"] = 0.20
    elif facts.scarcity and facts.scarcity.has_data:
        # Scarcity alone is weaker evidence than a real price, but it is not
        # nothing — it is what carries out-of-print books.
        total += 0.08
        parts["scarcity_only"] = 0.08

    if not insights.renown_abstained:
        total += 0.15
        parts["renown"] = 0.15

    if facts.synopsis:
        total += 0.10
        parts["synopsis"] = 0.10

    return clamp(total), parts


def score_book(
    product: Product,
    facts: BookFacts,
    insights: BookInsights,
    config: Config,
) -> ScoreBreakdown:
    ranking = config.ranking
    quality, quality_notes = quality_component(
        facts,
        ranking.get("bayes_prior_count", 50),
        category_baseline(product.categories, config),
    )
    renown, renown_notes = renown_component(facts, insights)
    value, value_notes = value_component(product, facts)
    conf, conf_notes = confidence(facts, insights)

    weights = {
        "quality": ranking.get("weight_quality", 0.50),
        "renown": ranking.get("weight_renown", 0.25),
        "value": ranking.get("weight_value", 0.25),
    }
    components = {
        "quality": quality,
        "renown": renown,
        "value": value,
    }

    # Missing components are dropped and the remaining weights renormalised,
    # rather than being scored as zero. Treating "unknown" as "bad" would bury
    # every book the sources happen not to cover.
    available = {k: v for k, v in components.items() if v is not None}
    weight_total = sum(weights[k] for k in available) or 1.0
    base = sum(weights[k] * v for k, v in available.items()) / weight_total

    # Renormalising alone is not enough. When only one component survives, the
    # composite is whatever that component says — a cheap unknown book with no
    # rating, no renown and no comps once scored 0.95 on price alone and topped
    # the ranking. So the composite is shrunk toward a neutral prior in
    # proportion to the evidence behind it: the same logic as the rating
    # shrinkage, applied one level up.
    evidence_weight = clamp(conf / EVIDENCE_SATURATION)
    shrunk = NEUTRAL_PRIOR + (base - NEUTRAL_PRIOR) * evidence_weight

    condition_factor = config.condition_factors.get(product.condition or "", 0.9)
    final = clamp(shrunk * condition_factor)

    return ScoreBreakdown(
        score=round(final, 4),
        quality=quality,
        renown=renown,
        value=value,
        condition_factor=condition_factor,
        confidence=round(conf, 3),
        notes={
            "base_before_shrinkage": round(base, 4),
            "evidence_weight": round(evidence_weight, 3),
            "quality": quality_notes,
            "renown": renown_notes,
            "value": value_notes,
            "confidence": conf_notes,
            "weights_used": {k: weights[k] for k in available},
            "missing_components": [k for k, v in components.items() if v is None],
            "condition": product.condition,
        },
    )
