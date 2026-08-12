"""Provider-agnostic LLM access.

OpenRouter (the default) and a local Ollama model differ only by the `provider`
and `model` values in config.toml, because both speak the OpenAI protocol. That
shared protocol is the whole abstraction, so this talks to it directly with
httpx — already a dependency — rather than through a client library. litellm
did this job once, but it brought 99 packages that nothing else here needed, and
everything it was relied on for (retries, backoff, model rotation, schema
validation) is implemented below rather than by it.

Structured output is enforced here rather than trusted. Free-tier models often
ignore `response_format`, so the schema goes in the prompt, the reply is parsed
defensively (fenced JSON, leading prose), validated with pydantic, and retried
with the validation error fed back on failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from pooks.config import Config

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# Ollama serves the OpenAI-compatible surface under /v1.
OLLAMA_SUFFIX = "/v1"


class LLMHTTPError(RuntimeError):
    """A non-2xx reply, carrying the status so backoff can react to it.

    Worth a type of its own: rate limiting used to be detected by string-matching
    "429" in an exception message, which is fragile and silently stops working if
    the provider rewords anything.
    """

    def __init__(self, status_code: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.retry_after = retry_after

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMUnavailableError(RuntimeError):
    """Raised when no provider could be reached or no valid output was produced."""


class LLMClient:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        fallback_model: str | None = None,
        extra_models: list[str] | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float = 0.2,
        timeout_s: float = 90.0,
        max_retries: int = 3,
    ) -> None:
        self.provider = provider
        self.model = model
        self.fallback_model = fallback_model
        self.extra_models = extra_models or []
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    @classmethod
    def from_config(cls, config: Config) -> LLMClient:
        llm = config.llm
        provider = llm.get("provider", "openrouter")

        if provider == "ollama":
            return cls(
                provider=provider,
                model=llm.get("ollama_model", "ollama/qwen2.5:3b"),
                fallback_model=None,
                api_base=llm.get("ollama_base_url"),
                temperature=llm.get("temperature", 0.2),
                timeout_s=llm.get("timeout_s", 90.0),
                max_retries=llm.get("max_retries", 3),
            )

        return cls(
            provider=provider,
            model=llm.get("model"),
            fallback_model=llm.get("fallback_model"),
            extra_models=llm.get("extra_models", []),
            api_key=config.secrets.openrouter_api_key,
            temperature=llm.get("temperature", 0.2),
            timeout_s=llm.get("timeout_s", 90.0),
            max_retries=llm.get("max_retries", 3),
        )

    def model_candidates(self) -> list[str]:
        """Models to try, in order, deduplicated."""
        ordered = [self.model, *self.extra_models, self.fallback_model]
        seen: list[str] = []
        for model in ordered:
            if model and model not in seen:
                seen.append(model)
        return seen or [self.model]

    @staticmethod
    def _backoff(error: Exception, attempt: int) -> float:
        """Seconds to wait before the next attempt.

        Retrying a rate limit immediately is pointless and impolite — an early
        version fired all three attempts inside one second against a provider
        that had just said 429.

        A provider's own Retry-After is honoured when present, since it knows
        better than any formula here.
        """
        if isinstance(error, LLMHTTPError) and error.retry_after is not None:
            return min(error.retry_after, 60.0)

        status = error.status_code if isinstance(error, LLMHTTPError) else None
        return (5.0 if _is_saturation(error, status) else 1.0) * (2**attempt)

    @property
    def available(self) -> bool:
        if self.provider == "ollama":
            return True
        return bool(self.api_key) and self.credential_problem() is None

    def credential_problem(self) -> str | None:
        """Explain why the configured credential cannot work, if it cannot.

        Worth the specificity: OpenRouter answers a malformed key with
        "Missing Authentication header", which says nothing about the cause. The
        common mistake is pasting a *model id* into OPENROUTER_API_KEY — they
        sit next to each other in the docs, and both are opaque strings.
        """
        if self.provider == "ollama":
            return None
        if not self.api_key:
            return "OPENROUTER_API_KEY is not set"

        key = self.api_key.strip()
        if "/" in key or key.endswith(":free"):
            return (
                f"OPENROUTER_API_KEY looks like a model id ({key!r}), not a key. "
                "Put the key (sk-or-v1-...) in OPENROUTER_API_KEY and set the "
                "model in config.toml under [llm].model."
            )
        if not key.startswith("sk-or-"):
            return (
                "OPENROUTER_API_KEY does not start with 'sk-or-'. Get a key at "
                "https://openrouter.ai/keys"
            )
        return None

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        max_retries: int | None = None,
    ) -> T:
        """Call the model and return a validated instance of `schema`."""
        retries = self.max_retries if max_retries is None else max_retries
        instructions = (
            f"{system}\n\n"
            "Reply with a single JSON object and nothing else — no prose, no "
            "code fences, no explanation outside the JSON. It must validate "
            f"against this JSON Schema:\n{json.dumps(schema.model_json_schema(), indent=2)}"
        )

        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user},
        ]

        last_error: Exception | None = None
        candidates = self.model_candidates()

        for attempt in range(retries):
            # Rotate through candidates rather than hammering two. Free-tier
            # models share provider pools and saturate independently, so the
            # next model is far more likely to succeed than the same one again.
            model = candidates[attempt % len(candidates)]

            try:
                raw = await self._complete(model, messages)
            except Exception as exc:  # noqa: BLE001 - provider errors vary wildly
                last_error = exc
                log.warning(
                    "llm call failed on %s (attempt %d/%d): %s",
                    model,
                    attempt + 1,
                    retries,
                    str(exc)[:180],
                )
                if attempt < retries - 1:
                    await asyncio.sleep(self._backoff(exc, attempt))
                continue

            try:
                return schema.model_validate(_extract_json(raw))
            except (ValidationError, ValueError) as exc:
                last_error = exc
                log.info(
                    "llm output failed validation (attempt %d/%d): %s",
                    attempt + 1,
                    retries,
                    str(exc)[:200],
                )
                # Feed the failure back so the retry is informed rather than blind.
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw[:2000]},
                    {
                        "role": "user",
                        "content": (
                            f"That did not validate: {str(exc)[:500]}. "
                            "Reply again with only the corrected JSON object."
                        ),
                    },
                ]

        raise LLMUnavailableError(f"no valid output after {retries} attempts: {last_error}")

    def _endpoint(self) -> str:
        if self.api_base:
            base = self.api_base.rstrip("/")
            if not base.endswith(OLLAMA_SUFFIX):
                base += OLLAMA_SUFFIX
            return f"{base}/chat/completions"
        return f"{OPENROUTER_BASE}/chat/completions"

    async def _complete(self, model: str, messages: list[dict[str, str]]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            # litellm required an "openrouter/" prefix to pick a provider; the
            # API itself wants the bare id.
            "model": model.removeprefix("openrouter/").removeprefix("ollama/"),
            "messages": messages,
            "temperature": self.temperature,
        }

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.post(self._endpoint(), headers=headers, json=payload)

        if response.status_code >= 400:
            raise LLMHTTPError(
                response.status_code,
                response.text[:400],
                _retry_after(response),
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMHTTPError(response.status_code, f"non-JSON reply: {exc}") from exc

        # Providers signal some failures with a 200 and an error body.
        if error := data.get("error"):
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            raise LLMHTTPError(int(code) if isinstance(code, int) else 502, message)

        choices = data.get("choices") or []
        if not choices:
            raise LLMHTTPError(502, f"no choices in reply: {str(data)[:200]}")
        return (choices[0].get("message") or {}).get("content") or ""


_SATURATION_MARKERS = (
    "rate",
    "resourceexhausted",
    "quota",
    "too many requests",
    "request limit reached",
    "overloaded",
    "capacity",
)


def _is_saturation(error: Exception, status: int | None) -> bool:
    """Whether a failure means "busy, try later" rather than "broken".

    The status code alone is not enough. OpenRouter relays upstream saturation
    as **502**, not 429 — observed as
    "HTTP 502: Upstream error from Nvidia: ResourceExhausted: Worker local total
    request limit reached (32/32)". Backing off one second from that just burns
    the retry budget, so the message is inspected as well.
    """
    if status in (429, 503):
        return True
    text = str(error).lower()
    return any(marker in text for marker in _SATURATION_MARKERS)


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a Retry-After header, if the provider sent a usable one."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # The HTTP-date form is legal but rare here, and guessing is worse than
        # falling back to the caller's own backoff.
        return None


def _extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply.

    Handles the three things models actually do: clean JSON, fenced JSON, and
    JSON preceded by a sentence of explanation.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty response")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if fenced := _FENCE.search(text):
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError(f"no JSON object found in response: {text[:200]}")
