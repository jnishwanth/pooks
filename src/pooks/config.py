"""Configuration loading: config.toml for tunables, .env for secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def project_root() -> Path:
    """Repo root, i.e. the directory holding config.toml."""
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    """Where config.toml lives.

    `POOKS_CONFIG` exists for packaged installs: under Nix the source tree is in
    the read-only store, so the config has to be able to live elsewhere.
    """
    if override := os.environ.get("POOKS_CONFIG"):
        return Path(override).expanduser()
    return project_root() / "config.toml"


def data_dir() -> Path:
    """Where the database and any writable state live.

    Same reason as above: the default alongside the source only works for a
    development checkout. A packaged service points this at its StateDirectory.
    """
    if override := os.environ.get("POOKS_DATA_DIR"):
        return Path(override).expanduser()
    return project_root() / "data"


def env_file() -> Path:
    """Optional .env. Absent for packaged installs, which inject the
    environment directly (systemd EnvironmentFile) rather than reading a file."""
    if override := os.environ.get("POOKS_ENV_FILE"):
        return Path(override).expanduser()
    return project_root() / ".env"


@dataclass(frozen=True)
class Secrets:
    searxng_url: str | None
    openrouter_api_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    hardcover_api_key: str | None
    google_books_api_key: str | None

    @classmethod
    def from_env(cls) -> Secrets:
        # Already-set environment variables win: a systemd EnvironmentFile is
        # the source of truth for a packaged install, and there is no .env there.
        load_dotenv(env_file(), override=False)

        def get(name: str) -> str | None:
            value = (os.environ.get(name) or "").strip()
            return value or None

        return cls(
            searxng_url=get("SEARXNG_URL"),
            openrouter_api_key=get("OPENROUTER_API_KEY"),
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=get("TELEGRAM_CHAT_ID"),
            hardcover_api_key=get("HARDCOVER_API_KEY"),
            google_books_api_key=get("GOOGLE_BOOKS_API_KEY"),
        )


@dataclass(frozen=True)
class Config:
    """Parsed config.toml plus secrets. Sections stay as plain dicts so adding a
    tunable to config.toml needs no code change here.

    The properties below coerce rather than merely annotate. A section is
    `dict[str, Any]` because it comes straight from TOML, so `get` cannot know
    the type — and TOML's own literals do not match the declaration either: a
    threshold written `push_score_threshold = 1` parses as an int. Coercing at
    the one place each default is read makes the annotation true instead of
    aspirational.
    """

    source: dict[str, Any]
    schedule: dict[str, Any]
    ratings: dict[str, Any]
    prices: dict[str, Any]
    matching: dict[str, Any]
    llm: dict[str, Any]
    ranking: dict[str, Any]
    notify: dict[str, Any]
    serve: dict[str, Any]
    backfill: dict[str, Any]
    secrets: Secrets = field(repr=False)

    @property
    def condition_factors(self) -> dict[str, float]:
        return dict(self.ranking.get("condition_factor", {}))

    @property
    def rating_chain(self) -> list[str]:
        """Rating sources in fallback order, best first.

        Empty is meaningful rather than a misconfiguration: emptying
        `[ratings].chain` is the documented way to turn rating lookup off.
        """
        return list(self.ratings.get("chain", []))

    @property
    def primary_rating_source(self) -> str | None:
        """The source a book's rating is *expected* to come from — anything else
        is a fallback worth repairing. None when the chain is empty, in which
        case no source is wrong because none was asked for."""
        return next(iter(self.rating_chain), None)

    @property
    def tags_askable(self) -> bool:
        """Whether the tag source can be asked at all.

        Hardcover is looked up with an API key, so without one a book that has
        no tags is not a gap anything can close — the same reason a book with no
        ISBN is recorded as settled rather than pending. The repair pass has to
        know, or every enriched book becomes a candidate and spends its retry
        budget on a lookup that cannot succeed.
        """
        return bool(self.secrets.hardcover_api_key)

    @property
    def refresh_min_score(self) -> float:
        """Score below which re-running the enrichment chain is not worth it.

        Read by the repair pass that spends the budget and by the health digest
        that reports how much repair work is outstanding; the two disagreeing
        would have the digest advertise books the daemon never picks up.
        """
        return float(self.schedule.get("refresh_min_score", 0.0))

    @property
    def prompt_version(self) -> int:
        """Bumping `[llm].prompt_version` invalidates every cached LLM response,
        so the writer and every reader of `llm_cache` must agree on it."""
        return int(self.llm.get("prompt_version", 1))

    @property
    def push_score_threshold(self) -> float:
        """Score a book must reach to be pushed. `pooks calibrate` tunes it."""
        return float(self.notify.get("push_score_threshold", 0.62))

    @property
    def push_min_confidence(self) -> float:
        """Confidence floor for a push: a high score computed from almost no
        evidence is not worth a notification."""
        return float(self.notify.get("push_min_confidence", 0.5))

    @property
    def max_books_per_message(self) -> int:
        return int(self.notify.get("max_books_per_message", 10))

    @property
    def db_path(self) -> Path:
        return data_dir() / "pooks.db"


@lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"config.toml not found at {path}. For a packaged install (Nix, or "
            "anywhere the source tree is read-only) set POOKS_CONFIG to its "
            "location; a development checkout expects it beside the source."
        )
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    return Config(
        source=raw["source"],
        schedule=raw["schedule"],
        ratings=raw["ratings"],
        prices=raw["prices"],
        matching=raw["matching"],
        llm=raw["llm"],
        ranking=raw["ranking"],
        notify=raw["notify"],
        serve=raw["serve"],
        backfill=raw.get("backfill", {}),
        secrets=Secrets.from_env(),
    )
