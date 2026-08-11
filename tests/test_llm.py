"""LLM plumbing: response parsing, the spoiler loop, and fail-safe behaviour.

These exercise everything that does not need a live model. Generation quality
itself requires a configured provider and is checked with `pooks probe-llm`.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from pooks.llm.client import LLMClient, LLMUnavailableError, _extract_json
from pooks.llm.roles import (
    Blurb,
    Renown,
    RenownTier,
    SpoilerVerdict,
    generate_blurb,
    judge_renown,
)


class _Schema(BaseModel):
    value: int


# --- response parsing ---------------------------------------------------------


def test_parses_clean_json() -> None:
    assert _extract_json('{"value": 1}') == {"value": 1}


def test_parses_fenced_json() -> None:
    """Models routinely wrap JSON in markdown fences despite instructions."""
    assert _extract_json('```json\n{"value": 2}\n```') == {"value": 2}
    assert _extract_json('```\n{"value": 3}\n```') == {"value": 3}


def test_parses_json_after_prose() -> None:
    text = 'Sure! Here is the JSON you asked for:\n\n{"value": 4}\n\nHope that helps.'
    assert _extract_json(text) == {"value": 4}


def test_rejects_unparseable_output() -> None:
    with pytest.raises(ValueError):
        _extract_json("no json here at all")
    with pytest.raises(ValueError):
        _extract_json("")


# --- retry with feedback ------------------------------------------------------


class FakeClient(LLMClient):
    """Replays canned responses in order, recording the prompts it received."""

    def __init__(self, responses: list[str]) -> None:
        super().__init__(provider="fake", model="fake/model", api_key="x")
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def _complete(self, model: str, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0) if self.responses else "{}"


async def test_invalid_output_is_retried_with_the_error_fed_back() -> None:
    client = FakeClient(['{"value": "not an int"}', '{"value": 7}'])

    result = await client.structured(system="s", user="u", schema=_Schema)

    assert result.value == 7
    assert len(client.calls) == 2
    # The retry must actually tell the model what went wrong.
    assert "did not validate" in client.calls[1][-1]["content"]


async def test_gives_up_after_max_retries() -> None:
    client = FakeClient(["garbage"] * 3)
    with pytest.raises(LLMUnavailableError):
        await client.structured(system="s", user="u", schema=_Schema, max_retries=3)


# --- the spoiler loop ---------------------------------------------------------


class ScriptedRoleClient(LLMClient):
    """Returns a queued object per structured() call, ignoring the prompt."""

    def __init__(self, queue: list[BaseModel]) -> None:
        super().__init__(provider="fake", model="fake/model", api_key="x")
        self.queue = list(queue)
        self.systems: list[str] = []

    async def structured(self, *, system, user, schema, max_retries=None):  # type: ignore[override]
        self.systems.append(system)
        return self.queue.pop(0)


async def test_spoiler_free_blurb_is_accepted_first_time() -> None:
    client = ScriptedRoleClient(
        [
            Blurb(blurb="A bleak, tightly written set of stories about survival."),
            SpoilerVerdict(has_spoilers=False),
        ]
    )

    blurb, verdict = await generate_blurb(
        client, title="T", author="A", synopsis="s", categories=[], rating=4.1,
        ratings_count=100,
    )

    assert verdict.has_spoilers is False
    assert "bleak" in blurb.blurb
    assert len(client.systems) == 2


async def test_flagged_blurb_is_regenerated_with_feedback() -> None:
    client = ScriptedRoleClient(
        [
            Blurb(blurb="It is brilliant until the narrator dies at the end."),
            SpoilerVerdict(has_spoilers=True, reason="reveals the narrator's death"),
            Blurb(blurb="A spare, unsettling account of life in the camps."),
            SpoilerVerdict(has_spoilers=False),
        ]
    )

    blurb, verdict = await generate_blurb(
        client, title="T", author="A", synopsis="s", categories=[], rating=None,
        ratings_count=None,
    )

    assert verdict.has_spoilers is False
    assert "dies" not in blurb.blurb
    # The regeneration prompt must carry the rejection reason.
    assert "narrator's death" in client.systems[2]


async def test_persistent_spoilers_suppress_the_blurb_entirely() -> None:
    """Returning text that keeps failing the check would defeat the point."""
    client = ScriptedRoleClient(
        [
            Blurb(blurb="spoiler one"),
            SpoilerVerdict(has_spoilers=True, reason="r1"),
            Blurb(blurb="spoiler two"),
            SpoilerVerdict(has_spoilers=True, reason="r2"),
        ]
    )

    blurb, _ = await generate_blurb(
        client, title="T", author=None, synopsis=None, categories=[], rating=None,
        ratings_count=None, max_attempts=2,
    )

    assert "spoiler one" not in blurb.blurb
    assert "spoiler two" not in blurb.blurb
    assert blurb.insufficient_context is True


# --- abstention and fail-safe -------------------------------------------------


async def test_renown_abstention_is_enforced_not_trusted() -> None:
    """A model claiming 'unknown' while still emitting a score must not have
    that score used — it would be invented prestige feeding the ranking."""
    client = ScriptedRoleClient(
        [Renown(tier=RenownTier.UNKNOWN, score=0.9, abstained=False, evidence="guess")]
    )

    renown = await judge_renown(
        client, title="T", author=None, publisher=None, year=None, categories=[],
        rating=None, ratings_count=None,
    )

    assert renown.abstained is True
    assert renown.score is None


async def test_renown_falls_back_to_abstention_when_llm_is_down() -> None:
    class DeadClient(LLMClient):
        async def structured(self, **kwargs):  # type: ignore[override]
            raise LLMUnavailableError("down")

    renown = await judge_renown(
        DeadClient(provider="fake", model="m", api_key="x"),
        title="T", author=None, publisher=None, year=None, categories=[],
        rating=None, ratings_count=None,
    )

    assert renown.abstained is True
    assert renown.tier is RenownTier.UNKNOWN


def test_client_reports_unavailable_without_credentials() -> None:
    assert LLMClient(provider="openrouter", model="m", api_key=None).available is False
    # A key must now look like a real one — a placeholder no longer counts as
    # configured, so the failure surfaces before any request is made.
    assert LLMClient(provider="openrouter", model="m", api_key="k").available is False
    assert (
        LLMClient(provider="openrouter", model="m", api_key="sk-or-v1-" + "a" * 64).available
        is True
    )
    assert LLMClient(provider="ollama", model="m").available is True


def test_model_id_pasted_as_api_key_is_caught() -> None:
    """OpenRouter answers a malformed key with "Missing Authentication header",
    which says nothing about the cause. Pasting a model id into the key field is
    an easy mistake — they sit together in the docs and both are opaque strings."""
    client = LLMClient(
        provider="openrouter", model="m", api_key="nvidia/nemotron-3-ultra-550b-a55b:free"
    )
    assert client.available is False
    assert "model id" in client.credential_problem()


def test_missing_key_is_reported_plainly() -> None:
    client = LLMClient(provider="openrouter", model="m", api_key=None)
    assert client.credential_problem() == "OPENROUTER_API_KEY is not set"


def test_wrong_prefix_is_caught() -> None:
    client = LLMClient(provider="openrouter", model="m", api_key="abc123def456")
    assert "sk-or-" in client.credential_problem()


def test_valid_looking_key_passes() -> None:
    client = LLMClient(provider="openrouter", model="m", api_key="sk-or-v1-" + "a" * 64)
    assert client.credential_problem() is None
    assert client.available is True


def test_ollama_needs_no_credential() -> None:
    assert LLMClient(provider="ollama", model="m").credential_problem() is None
