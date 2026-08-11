"""Scoring behaviour, especially the failure modes recon surfaced."""

from __future__ import annotations

import pytest

from pooks.config import load_config
from pooks.enrich.sources import BookFacts, IndianPrice, ScarcitySignal
from pooks.llm.pipeline import BookInsights
from pooks.models import Product
from pooks.rank.score import (
    NEUTRAL_PRIOR,
    bayesian_rating,
    confidence,
    score_book,
    value_component,
)


@pytest.fixture
def config():
    return load_config()


def _product(price_inr: float = 250, condition: str = "Very Good", **kwargs) -> Product:
    return Product(
        product_id=kwargs.pop("product_id", 1),
        name=kwargs.pop("name", "Some Book by Some Author"),
        price_paise=int(price_inr * 100),
        condition=condition,
        in_stock=True,
        **kwargs,
    )


def _facts(rating: float | None = None, count: int | None = None, **kwargs) -> BookFacts:
    return BookFacts(book_key="isbn:x", rating=rating, ratings_count=count, **kwargs)


# --- the headline ranking requirement ----------------------------------------


def test_thin_rating_cannot_outrank_a_well_established_one(config) -> None:
    """A 5.0 from 3 ratings must lose to a 4.2 from 20,000. This is the exact
    failure mode Open Library and per-edition Goodreads numbers produce."""
    product = _product()
    abstained = BookInsights(renown_abstained=True)

    thin = score_book(product, _facts(5.0, 3), abstained, config)
    established = score_book(product, _facts(4.2, 20_000), abstained, config)

    assert established.score > thin.score
    assert established.quality > thin.quality


def test_shrinkage_pulls_thin_samples_toward_the_mean() -> None:
    thin = bayesian_rating(5.0, 3, prior_count=50, global_mean=3.9)
    solid = bayesian_rating(5.0, 20_000, prior_count=50, global_mean=3.9)

    assert thin < 4.1, "3 ratings should barely move off the prior"
    assert solid > 4.9, "20,000 ratings should dominate the prior"


def test_higher_rating_still_wins_at_equal_volume(config) -> None:
    product = _product()
    insights = BookInsights(renown_abstained=True)

    better = score_book(product, _facts(4.5, 5_000), insights, config)
    worse = score_book(product, _facts(3.6, 5_000), insights, config)

    assert better.score > worse.score


# --- missing data must not be scored as bad ----------------------------------


def test_missing_components_are_renormalised_not_zeroed(config) -> None:
    """Treating 'unknown' as 'bad' would bury every book the sources miss."""
    product = _product()
    rated = _facts(4.2, 5_000)

    breakdown = score_book(product, rated, BookInsights(renown_abstained=True), config)

    assert "value" in breakdown.notes["missing_components"]
    assert "value" not in breakdown.notes["weights_used"]
    # Quality is strong, so the score should stay high despite the gap.
    assert breakdown.score > 0.5


def test_book_with_no_data_at_all_still_scores(config) -> None:
    breakdown = score_book(_product(), _facts(), BookInsights(), config)
    assert 0.0 <= breakdown.score <= 1.0
    assert breakdown.confidence < 0.2


def test_cheap_unknown_book_cannot_top_the_ranking(config) -> None:
    """Regression. Renormalising away missing components left a book with no
    rating, no renown and no comps scoring purely on price: it came out at 0.929
    and ranked first. Every digest would have been topped by cheap books nobody
    knows anything about."""
    unknown_and_cheap = score_book(_product(price_inr=150), _facts(), BookInsights(), config)
    known_and_good = score_book(
        _product(price_inr=450),
        _facts(4.13, 19_192, synopsis="s",
               indian_price=IndianPrice(price_paise=50_000, available_in_india=True)),
        BookInsights(renown_abstained=True),
        config,
    )

    assert unknown_and_cheap.score < known_and_good.score
    assert unknown_and_cheap.score < 0.5


def test_no_evidence_regresses_toward_the_neutral_prior(config) -> None:
    breakdown = score_book(_product(price_inr=100), _facts(), BookInsights(), config)
    assert breakdown.notes["evidence_weight"] == 0.0
    # Only the condition factor separates it from the prior.
    assert abs(breakdown.score - NEUTRAL_PRIOR * breakdown.condition_factor) < 1e-6


def test_well_evidenced_scores_are_not_shrunk(config) -> None:
    breakdown = score_book(
        _product(),
        _facts(4.2, 40_000, synopsis="s",
               indian_price=IndianPrice(price_paise=50_000, available_in_india=True)),
        BookInsights(renown_abstained=False, renown_score=0.8),
        config,
    )
    assert breakdown.notes["evidence_weight"] == 1.0


# --- value uses landed cost, not sticker price -------------------------------


def test_value_is_measured_against_the_indian_price() -> None:
    """The baseline is what the buyer would otherwise pay locally. Comparing
    against AbeBooks in USD rated every book 87-91% cheaper — a constant."""
    product = _product(price_inr=220)
    facts = _facts(indian_price=IndianPrice(price_paise=33_630, source="amazon.in",
                                            available_in_india=True))

    value, notes = value_component(product, facts)

    assert value is not None
    assert notes["india_inr"] == 336.3
    assert 0.30 < notes["saving"] < 0.40   # Rs 220 vs Rs 336 is ~35% under


def test_out_of_print_without_indian_price_gets_scarcity_credit() -> None:
    value, notes = value_component(_product(), _facts(in_print=False))
    assert value == 0.7
    assert "out of print" in notes["basis"]


def test_not_sold_in_india_is_mildly_positive() -> None:
    """Import-only means a local second-hand copy is genuinely valuable."""
    facts = _facts(indian_price=IndianPrice(available_in_india=False, unknown=False))
    value, notes = value_component(_product(), facts)
    assert value == 0.7
    assert "not sold in india" in notes["basis"]


def test_blocked_india_lookup_is_unknown_not_scarce() -> None:
    """A blocked lookup also leaves available_in_india False. Scoring it as
    scarcity would reward a network failure with a better ranking."""
    facts = _facts(indian_price=IndianPrice(available_in_india=False, unknown=True))
    value, notes = value_component(_product(), facts)
    assert value is None
    assert "unknown" in notes["basis"]


def test_deeper_discount_scores_better_value() -> None:
    india = IndianPrice(price_paise=100_000, source="amazon.in", available_in_india=True)
    deep, _ = value_component(_product(price_inr=400), _facts(indian_price=india))   # 60% under
    shallow, _ = value_component(_product(price_inr=900), _facts(indian_price=india))  # 10% under
    assert deep > shallow


def test_scarcity_lifts_value_at_equal_price() -> None:
    india = IndianPrice(price_paise=100_000, source="amazon.in", available_in_india=True)
    rare, _ = value_component(
        _product(price_inr=400),
        _facts(indian_price=india, scarcity=ScarcitySignal(listing_count=1)),
    )
    common, _ = value_component(
        _product(price_inr=400),
        _facts(indian_price=india, scarcity=ScarcitySignal(listing_count=40)),
    )
    assert rare > common


# --- confidence gating --------------------------------------------------------


def test_confidence_reflects_evidence_volume() -> None:
    thin, _ = confidence(_facts(4.5, 3), BookInsights(renown_abstained=True))
    rich, _ = confidence(
        _facts(4.2, 40_000, synopsis="a synopsis",
               indian_price=IndianPrice(price_paise=50_000, available_in_india=True)),
        BookInsights(renown_abstained=False, renown_score=0.8),
    )
    assert rich > thin
    assert rich > 0.7
    assert thin < 0.45


def test_no_evidence_means_no_confidence() -> None:
    value, _ = confidence(_facts(), BookInsights())
    assert value == 0.0


# --- condition ---------------------------------------------------------------


def test_condition_downgrades_score(config) -> None:
    facts = _facts(4.2, 5_000)
    insights = BookInsights(renown_abstained=True)

    good = score_book(_product(condition="New"), facts, insights, config)
    fair = score_book(_product(condition="Fair"), facts, insights, config)

    assert good.score > fair.score
