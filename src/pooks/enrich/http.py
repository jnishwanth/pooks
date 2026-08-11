"""Shared HTTP client for enrichment sources.

Enrichment fans out to several third-party hosts. Each gets its own rate limit
so a burst against one never becomes a burst against all, and every request
carries a browser-like User-Agent.

The other half of this module is throttle handling, which is not optional.
Goodreads sits behind AWS WAF and answers a client it dislikes with **HTTP 202
and a zero-length body** rather than 429 — a response that looks successful and
parses to "no rating found" unless you check for it explicitly. Roughly ten
requests in a few minutes is enough to trigger it, and the block then persists
for about six minutes. Hence the long default interval and the per-host circuit
breaker below.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# A realistic User-Agent alone is not enough. Amazon.in answers a request
# carrying only UA and Accept-Language with a ~2KB stub containing no prices,
# which reads exactly like a bot wall — that stub is what led to Amazon being
# written off as unusable during planning. With the headers below the same URL
# returns 130-620KB of real results. Anything scraping HTML needs the full set.
BROWSER_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Per-host minimum seconds between requests. Goodreads is deliberately slow:
# steady state is ~15 books/day, so 60s spacing costs 15 minutes of wall clock
# a day and keeps us far below the mitigation threshold.
HOST_INTERVALS: dict[str, float] = {
    "www.goodreads.com": 60.0,
    # Measured: Amazon 503s under sustained volume. 30s was not enough after a
    # few dozen cumulative requests, and once tripped it stayed blocked for
    # several minutes. It recovers fully, so the answer is patience rather than
    # evasion. At ~15 books/day this costs ~20 minutes of wall clock.
    "www.amazon.in": 90.0,
    "www.abebooks.com": 6.0,
    "www.bookswagon.com": 5.0,
    "bookstohome.co.in": 5.0,
    "www.thebookx.in": 5.0,
    "openlibrary.org": 1.0,
    "www.googleapis.com": 1.0,
    "api.hardcover.app": 1.0,
    "api.frankfurter.dev": 1.0,
}
DEFAULT_INTERVAL = 2.0

# Minimum plausible body size for hosts we scrape HTML from. A 200 response far
# below this is a stub or interstitial, not a real page — the failure mode that
# made Amazon.in look permanently walled. JSON APIs are absent here because a
# small JSON body is perfectly normal.
HOST_MIN_BYTES: dict[str, int] = {
    "www.amazon.in": 20_000,
    "www.goodreads.com": 10_000,
    "www.abebooks.com": 10_000,
}

# Consecutive soft blocks before a host is taken out of rotation.
BREAKER_THRESHOLD = 2
# How long to leave it out. Observed recovery is ~6 minutes; 15 gives headroom.
BREAKER_COOLDOWN_S = 900.0


@dataclass
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float = 0.0
    consecutive_blocks: int = 0
    cooldown_until: float = 0.0


class PoliteClient:
    def __init__(self, timeout_s: float = 30.0, user_agent: str = BROWSER_UA) -> None:
        self._client = httpx.AsyncClient(
            headers=BROWSER_HEADERS | {"User-Agent": user_agent},
            timeout=timeout_s,
            follow_redirects=True,
        )
        self._hosts: dict[str, _HostState] = defaultdict(_HostState)

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def degraded_hosts(self) -> list[str]:
        """Hosts that soft-blocked us or are in cooldown.

        Callers use this to distinguish "this book genuinely has no rating"
        from "we were blocked before we could find out" — the two must not be
        cached with the same lifetime.
        """
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:  # pragma: no cover - only outside a loop
            now = 0.0
        return [
            host
            for host, state in self._hosts.items()
            if state.consecutive_blocks > 0 or state.cooldown_until > now
        ]

    async def get(self, url: str, **kwargs: object) -> httpx.Response | None:
        return await self._request("GET", url, **kwargs)

    async def post_json(
        self, url: str, json_body: dict[str, object], headers: dict[str, str] | None = None
    ) -> httpx.Response | None:
        return await self._request("POST", url, json=json_body, headers=headers or {})

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response | None:
        host = urlparse(url).netloc
        state = self._hosts[host]
        interval = HOST_INTERVALS.get(host, DEFAULT_INTERVAL)

        async with state.lock:
            loop = asyncio.get_running_loop()

            if state.cooldown_until > loop.time():
                remaining = state.cooldown_until - loop.time()
                log.debug("%s in cooldown for another %.0fs; skipping", host, remaining)
                return None

            # Jitter so requests do not arrive on a perfectly regular cadence,
            # which is itself a bot signature.
            wait = state.last_request + interval * random.uniform(1.0, 1.25) - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            state.last_request = asyncio.get_running_loop().time()

        try:
            response = await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            log.warning("fetch failed %s: %s", url, exc)
            return None

        if _is_soft_block(response, HOST_MIN_BYTES.get(host)):
            state.consecutive_blocks += 1
            log.warning(
                "%s soft-blocked us (HTTP %d, %d bytes) — attempt %d/%d",
                host,
                response.status_code,
                len(response.content),
                state.consecutive_blocks,
                BREAKER_THRESHOLD,
            )
            if state.consecutive_blocks >= BREAKER_THRESHOLD:
                state.cooldown_until = (
                    asyncio.get_running_loop().time() + BREAKER_COOLDOWN_S
                )
                log.error(
                    "%s taken out of rotation for %.0f minutes. Enrichment will "
                    "fall through to the remaining sources.",
                    host,
                    BREAKER_COOLDOWN_S / 60,
                )
            return None

        state.consecutive_blocks = 0

        if response.status_code >= 400:
            log.info("fetch %s -> HTTP %d", url, response.status_code)
            return None
        return response


def _is_soft_block(response: httpx.Response, min_bytes: int | None = None) -> bool:
    """Detect a bot-mitigation response dressed up as a success.

    Two shapes, both of which parse as a legitimate "nothing found" and would
    otherwise be cached as a permanent miss:

      * Goodreads/CloudFront returns 202 with content-length 0.
      * Amazon.in returns 200 with a ~2KB stub carrying no results.
    """
    if response.status_code in (429, 503):
        return True
    if response.status_code == 202 and not response.content:
        return True
    if (
        min_bytes is not None
        and response.status_code == 200
        and len(response.content) < min_bytes
    ):
        return True
    return False
