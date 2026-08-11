"""Config loading.

Mostly a guard against TOML's subsection rule: any plain key written after a
`[section.subsection]` header belongs to the subsection, not the parent. Adding
`[ratings.min_count_by_source]` above `chain` silently emptied the rating chain
— the pipeline kept running and simply stopped resolving any ratings.
"""

from __future__ import annotations

from pooks.config import load_config


def test_every_section_is_present() -> None:
    config = load_config()
    for section in (
        "source",
        "schedule",
        "ratings",
        "prices",
        "matching",
        "llm",
        "ranking",
        "notify",
        "serve",
    ):
        assert getattr(config, section), f"[{section}] missing or empty"


def test_rating_chain_survives_the_subsection() -> None:
    """The exact regression: `chain` must stay in [ratings], not get reparented
    into [ratings.min_count_by_source]."""
    ratings = load_config().ratings
    assert ratings.get("chain"), "[ratings].chain was lost — check key ordering"
    assert "goodreads" in ratings["chain"]
    assert isinstance(ratings.get("min_ratings_count"), int)


def test_per_source_floors_are_loaded() -> None:
    floors = load_config().ratings.get("min_count_by_source", {})
    assert floors.get("hardcover", 999) < floors.get("goodreads", 0), (
        "Hardcover's community is far smaller than Goodreads', so its floor "
        "must be lower or its ratings are always discarded"
    )


def test_india_price_subsection_is_nested_not_flattened() -> None:
    prices = load_config().prices
    assert prices.get("abebooks_enabled") is not None, "[prices] keys were reparented"
    assert prices.get("india", {}).get("sources"), "[prices.india].sources missing"


def test_ranking_weights_are_sane() -> None:
    ranking = load_config().ranking
    total = sum(
        ranking[k]
        for k in ("weight_quality", "weight_renown", "weight_value", "weight_affordability")
    )
    assert abs(total - 1.0) < 1e-9, f"weights should sum to 1.0, got {total}"
    assert ranking["weight_quality"] > ranking["weight_renown"], "rating must lead"
