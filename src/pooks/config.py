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
        load_dotenv(project_root() / ".env")

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
    tunable to config.toml needs no code change here."""

    source: dict[str, Any]
    schedule: dict[str, Any]
    ratings: dict[str, Any]
    prices: dict[str, Any]
    matching: dict[str, Any]
    llm: dict[str, Any]
    ranking: dict[str, Any]
    notify: dict[str, Any]
    serve: dict[str, Any]
    secrets: Secrets = field(repr=False)

    @property
    def condition_factors(self) -> dict[str, float]:
        return self.ranking.get("condition_factor", {})

    @property
    def db_path(self) -> Path:
        return project_root() / "data" / "pooks.db"


@lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> Config:
    path = path or project_root() / "config.toml"
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
        secrets=Secrets.from_env(),
    )
