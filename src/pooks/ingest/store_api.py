"""WooCommerce Store API client.

The shop exposes /wp-json/wc/store/v1/products publicly, which makes HTML
scraping unnecessary. Two endpoints matter:

  wc/store/v1/products  structured product data, supports ?stock_status=instock
  wp/v2/product         creation/modification timestamps, absent from the Store API

Politeness: a descriptive User-Agent, a minimum interval between requests, and
conditional GETs so an idle poll transfers no body.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from pooks.models import Product

log = logging.getLogger(__name__)

STORE_PRODUCTS = "/wp-json/wc/store/v1/products"
WP_PRODUCTS = "/wp-json/wp/v2/product"
MAX_PER_PAGE = 100


def _in_stock_page(page_size: int, page: int = 1) -> dict[str, Any]:
    """The window the poll and the sweep both read: in stock, newest first.

    One definition because the two have to agree. The poll compares the
    `x-wp-total` this filter reports against the count the last *sweep* stored,
    and its max product id against the sweep's, so a filter differing by a
    single key would either report a change on every poll or never report one.
    The ordering is load-bearing for the same reason: the poll's max id is the
    catalogue maximum only because page 1 of a newest-first list holds the
    newest ids.
    """
    return {
        "per_page": page_size,
        "page": page,
        "orderby": "date",
        "order": "desc",
        "stock_status": "instock",
    }


@dataclass
class PollResult:
    """Outcome of a cheap poll.

    `changed` is deliberately derived from three independent signals. The
    Last-Modified header is the cheapest, but we do not trust it alone: if it
    fails to advance when stock changes, the in-stock total and the maximum
    product id still catch it.
    """

    changed: bool
    not_modified: bool
    last_modified: str | None = None
    instock_total: int | None = None
    max_product_id: int | None = None
    products: list[Product] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


class RateLimiter:
    """Enforces a minimum interval between outbound requests."""

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval_s = min_interval_s
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._last + self.min_interval_s - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_running_loop().time()


class StoreAPIClient:
    def __init__(
        self,
        base_url: str,
        user_agent: str,
        *,
        min_request_interval_s: float = 2.0,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._limiter = RateLimiter(min_request_interval_s)
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout_s,
            follow_redirects=True,
        )

    async def __aenter__(self) -> StoreAPIClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        )
        async def _attempt() -> httpx.Response:
            await self._limiter.acquire()
            response = await self._client.get(url, **kwargs)
            # 304 is a successful outcome for a conditional GET, not an error.
            if response.status_code >= 500:
                response.raise_for_status()
            return response

        return await _attempt()

    # -------------------------------------------------------------------- poll

    async def poll(
        self,
        *,
        last_modified: str | None = None,
        known_total: int | None = None,
        known_max_id: int | None = None,
        page_size: int = 20,
    ) -> PollResult:
        """Cheap change check against the newest in-stock listings."""
        headers = {"If-Modified-Since": last_modified} if last_modified else {}
        response = await self._get(
            STORE_PRODUCTS, params=_in_stock_page(page_size), headers=headers
        )

        if response.status_code == 304:
            return PollResult(changed=False, not_modified=True, last_modified=last_modified)

        response.raise_for_status()
        payload = response.json()
        products = [Product.from_store_api(item) for item in payload]

        new_last_modified = response.headers.get("last-modified")
        total = _int_header(response.headers, "x-wp-total")
        max_id = max((p.product_id for p in products), default=None)

        reasons: list[str] = []
        if last_modified is None:
            reasons.append("first-poll")
        if new_last_modified and new_last_modified != last_modified:
            reasons.append("last-modified-advanced")
        if total is not None and known_total is not None and total != known_total:
            reasons.append(f"instock-total {known_total}->{total}")
        if max_id is not None and known_max_id is not None and max_id != known_max_id:
            reasons.append(f"max-id {known_max_id}->{max_id}")

        return PollResult(
            changed=bool(reasons),
            not_modified=False,
            last_modified=new_last_modified,
            instock_total=total,
            max_product_id=max_id,
            products=products,
            reasons=reasons,
        )

    # ------------------------------------------------------------------ sweeps

    async def fetch_in_stock(
        self, page_size: int = MAX_PER_PAGE
    ) -> tuple[list[Product], str | None]:
        """Every in-stock product, plus the Last-Modified header.

        The header comes back so the caller can tell a genuinely unreliable
        signal (it did not move despite a real change) from a poll that simply
        has not run since the change.
        """
        collected: list[Product] = []
        last_modified: str | None = None
        page = 1
        total_pages: int | None = None

        while True:
            response = await self._get(STORE_PRODUCTS, params=_in_stock_page(page_size, page))
            response.raise_for_status()
            last_modified = response.headers.get("last-modified", last_modified)
            batch = response.json()
            if not batch:
                break

            collected.extend(Product.from_store_api(item) for item in batch)

            if total_pages is None:
                total = _int_header(response.headers, "x-wp-total")
                total_pages = -(-total // page_size) if total else None

            if len(batch) < page_size or (total_pages is not None and page >= total_pages):
                break
            page += 1

        return collected, last_modified

    async def fetch_dates(self, product_ids: list[int]) -> dict[int, dict[str, str]]:
        """Creation/modification timestamps, which the Store API omits.

        Batched through wp/v2 `include`, which caps at 100 ids per request.
        """
        dates: dict[int, dict[str, str]] = {}
        for start in range(0, len(product_ids), MAX_PER_PAGE):
            chunk = product_ids[start : start + MAX_PER_PAGE]
            try:
                response = await self._get(
                    WP_PRODUCTS,
                    params={
                        "include": ",".join(str(i) for i in chunk),
                        "per_page": len(chunk),
                        "_fields": "id,date_gmt,modified_gmt",
                    },
                )
                response.raise_for_status()
                for item in response.json():
                    if isinstance(item, dict) and "id" in item:
                        dates[item["id"]] = {
                            "date_created": item.get("date_gmt"),
                            "date_modified": item.get("modified_gmt"),
                        }
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                # Dates are useful but not load-bearing; ingest proceeds without.
                #
                # Reading the body is inside the guard, not just fetching it. A
                # 200 can carry HTML (a WAF interstitial, a login redirect),
                # which fails in the decode rather than in the status; or a WP
                # REST error envelope — `{"code": "rest_forbidden", ...}` served
                # with a 200 by a cache — which decodes cleanly, whereupon
                # iterating it yields the dict's *keys*. `cli.cmd_sweep` calls
                # `backfill_dates` with no equivalent of the daemon's guard, so
                # anything escaping here is a traceback for the operator and the
                # remaining chunks abandoned.
                log.warning("wp/v2 date fetch failed for %d ids: %s", len(chunk), exc)
                continue
        return dates


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, TypeError, ValueError):
        return None
