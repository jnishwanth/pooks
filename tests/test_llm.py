"""LLM plumbing: response parsing, the spoiler loop, and fail-safe behaviour.

These exercise everything that does not need a live model. Generation quality
itself requires a configured provider and is checked with `pooks probe-llm`.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from pooks.llm.client import (
    LLMClient,
    LLMHTTPError,
    LLMUnavailableError,
    _extract_json,
)
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


def test_rejects_valid_json_that_is_not_an_object() -> None:
    """A bare array or scalar parses cleanly but is not a schema instance.

    Returning it handed pydantic something it could only report as a confusing
    validation error; raising sends it down the same retry-with-feedback path
    as a parse failure, which is the one that tells the model what to fix.
    """
    for reply in ('["a", "b"]', '"just a string"', "42", "null"):
        with pytest.raises(ValueError):
            _extract_json(reply)


def test_an_object_is_still_found_inside_a_leading_array() -> None:
    """Rejecting a non-object must not abandon the later strategies: the brace
    scan can still recover the real payload."""
    assert _extract_json('[1, 2]\n{"value": 5}') == {"value": 5}


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
        client,
        title="T",
        author="A",
        synopsis="s",
        categories=[],
        rating=4.1,
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
        client,
        title="T",
        author="A",
        synopsis="s",
        categories=[],
        rating=None,
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
        client,
        title="T",
        author=None,
        synopsis=None,
        categories=[],
        rating=None,
        ratings_count=None,
        max_attempts=2,
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
        client,
        title="T",
        author=None,
        publisher=None,
        year=None,
        categories=[],
        rating=None,
        ratings_count=None,
    )

    assert renown.abstained is True
    assert renown.score is None


async def test_renown_falls_back_to_abstention_when_llm_is_down() -> None:
    class DeadClient(LLMClient):
        async def structured(self, **kwargs):  # type: ignore[override]
            raise LLMUnavailableError("down")

    renown = await judge_renown(
        DeadClient(provider="fake", model="m", api_key="x"),
        title="T",
        author=None,
        publisher=None,
        year=None,
        categories=[],
        rating=None,
        ratings_count=None,
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


# --- transport: HTTP errors and backoff --------------------------------------


def test_openrouter_502_saturation_backs_off_like_a_rate_limit() -> None:
    """OpenRouter relays upstream saturation as 502, not 429 — observed as
    "Upstream error from Nvidia: ResourceExhausted: Worker local total request
    limit reached (32/32)". Treating that as an ordinary error backs off one
    second and burns the retry budget against a provider that is simply busy."""
    err = LLMHTTPError(502, "Upstream error from Nvidia: ResourceExhausted: (32/32)")
    ordinary = LLMHTTPError(500, "internal error")

    assert LLMClient._backoff(err, 0) > LLMClient._backoff(ordinary, 0)
    assert LLMClient._backoff(err, 0) == 5.0


def test_explicit_rate_limit_statuses_back_off_hard() -> None:
    for status in (429, 503):
        assert LLMClient._backoff(LLMHTTPError(status, "slow down"), 0) == 5.0


def test_retry_after_header_is_honoured_over_the_formula() -> None:
    """The provider knows its own recovery window better than any formula."""
    assert LLMClient._backoff(LLMHTTPError(429, "wait", retry_after=12.0), 3) == 12.0
    # ...but not unboundedly: a huge value would stall the pipeline.
    assert LLMClient._backoff(LLMHTTPError(429, "wait", retry_after=9999.0), 0) == 60.0


def test_backoff_grows_with_attempts() -> None:
    err = LLMHTTPError(500, "boom")
    assert [LLMClient._backoff(err, i) for i in range(4)] == [1.0, 2.0, 4.0, 8.0]


def test_model_prefix_is_stripped_for_the_api() -> None:
    """litellm needed an "openrouter/" prefix to route; the API wants the bare id."""
    client = LLMClient(
        provider="openrouter",
        model="openrouter/nvidia/nemotron:free",
        api_key="sk-or-v1-" + "a" * 64,
    )
    assert client._endpoint() == "https://openrouter.ai/api/v1/chat/completions"


def test_ollama_endpoint_gets_the_openai_suffix() -> None:
    client = LLMClient(
        provider="ollama", model="ollama/gemma3:4b", api_base="http://localhost:11434"
    )
    assert client._endpoint() == "http://localhost:11434/v1/chat/completions"
    # Already-suffixed bases must not be doubled.
    already = LLMClient(provider="ollama", model="m", api_base="http://localhost:11434/v1")
    assert already._endpoint() == "http://localhost:11434/v1/chat/completions"


# --- failures must not be cached ---------------------------------------------


class _StoreSpy:
    """Records what would be written to llm_cache."""

    def __init__(self, existing: dict | None = None) -> None:
        self.written: list[tuple[str, str]] = []
        self.existing = existing or {}
        self.conn = None

    def get_llm(self, book_key, role, version):
        return self.existing.get((book_key, str(role)))

    def put_llm(self, book_key, role, version, payload, model=None):
        self.written.append((str(role), payload))


async def test_an_empty_blurb_is_not_cached(monkeypatch) -> None:
    """A rate-limited call returns empty text. Caching that pinned the book to a
    blank blurb permanently — the only escape being a prompt_version bump, which
    discards every role for every book. Eight books were stuck this way."""
    import pooks.llm.pipeline as pipe
    from pooks.enrich.sources import BookFacts
    from pooks.llm.pipeline import InsightGenerator
    from pooks.models import Product

    async def dead_blurb(*args, **kwargs):
        raise LLMUnavailableError("rate limited")

    async def ok_renown(*args, **kwargs):
        return Renown(tier=RenownTier.MAJOR, score=0.8, abstained=False)

    monkeypatch.setattr(pipe, "generate_blurb", dead_blurb)
    monkeypatch.setattr(pipe, "judge_renown", ok_renown)
    monkeypatch.setattr(pipe, "_store", lambda store, k, r, v, p, m: store.put_llm(k, r, v, p, m))

    store = _StoreSpy()
    generator = InsightGenerator(
        LLMClient(provider="openrouter", model="m", api_key="sk-or-v1-" + "a" * 64), 1
    )
    await generator.generate(
        store,
        Product(product_id=1, name="T"),
        BookFacts(book_key="isbn:1", synopsis="Enough retrieved text to ground it."),
    )

    roles = [role for role, _ in store.written]
    assert "blurb" not in roles, "an empty blurb must be retried, not cached"
    assert "renown" in roles, "the renown result was fine and should persist"


async def test_an_unavailable_renown_is_not_cached(monkeypatch) -> None:
    """A genuine abstention is a real answer worth keeping; one caused by an
    unreachable model is not."""
    import pooks.llm.pipeline as pipe
    from pooks.enrich.sources import BookFacts
    from pooks.llm.pipeline import InsightGenerator
    from pooks.models import Product

    async def ok_blurb(*args, **kwargs):
        return Blurb(blurb="A real blurb."), SpoilerVerdict(has_spoilers=False)

    async def dead_renown(*args, **kwargs):
        return Renown(
            tier=RenownTier.UNKNOWN, abstained=True, evidence="LLM unavailable", unavailable=True
        )

    monkeypatch.setattr(pipe, "generate_blurb", ok_blurb)
    monkeypatch.setattr(pipe, "judge_renown", dead_renown)
    monkeypatch.setattr(pipe, "_store", lambda store, k, r, v, p, m: store.put_llm(k, r, v, p, m))

    store = _StoreSpy()
    generator = InsightGenerator(
        LLMClient(provider="openrouter", model="m", api_key="sk-or-v1-" + "a" * 64), 1
    )
    await generator.generate(
        store,
        Product(product_id=1, name="T"),
        BookFacts(book_key="isbn:1", synopsis="Enough retrieved text to ground it."),
    )

    roles = [role for role, _ in store.written]
    assert "renown" not in roles
    assert "blurb" in roles


def test_a_genuine_abstention_is_still_cacheable() -> None:
    """Only failure is excluded — an honest 'I cannot tell' is a real answer."""
    assert Renown(tier=RenownTier.UNKNOWN, abstained=True).unavailable is False


async def test_a_blurb_is_not_attempted_without_grounding(monkeypatch) -> None:
    """The design is explicit that blurbs are grounded in retrieved text rather
    than recalled. With no synopsis the model padded with metadata the digest
    card already shows — "categorized as history and non-fiction. With a 3.77/5
    rating from 337 readers". Half the enriched books had no synopsis, so this
    was half the output."""
    import pooks.llm.pipeline as pipe
    from pooks.enrich.sources import BookFacts
    from pooks.llm.pipeline import InsightGenerator
    from pooks.models import Product

    called = False

    async def should_not_run(*args, **kwargs):
        nonlocal called
        called = True
        return Blurb(blurb="filler"), SpoilerVerdict(has_spoilers=False)

    async def ok_renown(*args, **kwargs):
        return Renown(tier=RenownTier.MAJOR, score=0.8, abstained=False)

    monkeypatch.setattr(pipe, "generate_blurb", should_not_run)
    monkeypatch.setattr(pipe, "judge_renown", ok_renown)
    monkeypatch.setattr(pipe, "_store", lambda store, k, r, v, p, m: store.put_llm(k, r, v, p, m))

    store = _StoreSpy()
    generator = InsightGenerator(
        LLMClient(provider="openrouter", model="m", api_key="sk-or-v1-" + "a" * 64), 1
    )
    insights = await generator.generate(
        store, Product(product_id=1, name="T"), BookFacts(book_key="isbn:1")
    )

    assert called is False, "no synopsis means no call at all"
    assert not insights.blurb
    # Renown is grounded in metadata we do have, so it still runs.
    assert "renown" in [role for role, _ in store.written]
