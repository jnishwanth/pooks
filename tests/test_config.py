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
        for k in ("weight_quality", "weight_renown", "weight_value")
    )
    assert abs(total - 1.0) < 1e-9, f"weights should sum to 1.0, got {total}"
    assert ranking["weight_quality"] > ranking["weight_renown"], "rating must lead"


def test_tunables_read_by_more_than_one_module_have_one_definition() -> None:
    """`prompt_version` and the push thresholds are each read from several
    modules. They were inline `.get(key, default)` calls at every site, so a
    default only had to disagree in one of them for the writer and the reader to
    stop matching — a stale `prompt_version` reader, for instance, would see a
    permanently empty LLM cache."""
    config = load_config()

    assert config.prompt_version == config.llm["prompt_version"]
    assert config.push_score_threshold == config.notify["push_score_threshold"]
    assert config.push_min_confidence == config.notify["push_min_confidence"]
    assert config.max_books_per_message == config.notify["max_books_per_message"]


def test_tunables_fall_back_when_the_key_is_absent() -> None:
    """The defaults have to survive a config.toml written before the key
    existed, which is the only reason they are in the code at all."""
    from dataclasses import replace

    bare = replace(load_config(), llm={}, notify={})

    assert bare.prompt_version == 1
    assert bare.push_score_threshold == 0.62
    assert bare.push_min_confidence == 0.5
    assert bare.max_books_per_message == 10


def test_tags_are_askable_only_with_a_key_to_ask_with() -> None:
    """Read by the repair pass to decide whether an untagged book is a gap it
    can close. Without a key it is not, and selecting those books would spend
    the retry budget on a lookup that cannot succeed."""
    from dataclasses import replace

    config = load_config()

    assert replace(config, secrets=replace(config.secrets, hardcover_api_key="k")).tags_askable
    assert not replace(config, secrets=replace(config.secrets, hardcover_api_key=None)).tags_askable
    assert not replace(config, secrets=replace(config.secrets, hardcover_api_key="")).tags_askable


def test_config_path_honours_the_environment_override(tmp_path, monkeypatch) -> None:
    """Packaged installs need this: under Nix the source tree is in the
    read-only store, so config.toml and the database must live elsewhere."""
    from pooks.config import config_path, data_dir

    monkeypatch.setenv("POOKS_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.setenv("POOKS_DATA_DIR", str(tmp_path / "state"))

    assert config_path() == tmp_path / "c.toml"
    assert data_dir() == tmp_path / "state"


def test_missing_config_explains_the_override(tmp_path, monkeypatch) -> None:
    """The failure mode is a package imported from an install path with no
    config beside it; the error has to name the way out."""
    import pytest

    from pooks.config import load_config

    monkeypatch.setenv("POOKS_CONFIG", str(tmp_path / "absent.toml"))
    load_config.cache_clear()
    try:
        with pytest.raises(FileNotFoundError, match="POOKS_CONFIG"):
            load_config()
    finally:
        load_config.cache_clear()
