"""The Indian price baseline.

Fixtures are real Amazon.in responses captured during development: a hit
(ISBN 9780140020304, one organic result at ₹336.30) and a miss
(ISBN 9780571166800, a Faber paperback genuinely not sold in India).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pooks.enrich.http import _is_soft_block
from pooks.enrich.indian_prices import (
    fetch_indian_price,
    parse_amazon,
    parse_retailer,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def amazon_hit() -> str:
    return (FIXTURES / "amazon_in_hit.html").read_text()


@pytest.fixture
def amazon_miss() -> str:
    return (FIXTURES / "amazon_in_miss.html").read_text()


# --- Amazon parsing -----------------------------------------------------------


def test_parses_price_from_real_amazon_response(amazon_hit: str) -> None:
    candidate = parse_amazon(amazon_hit)
    assert candidate is not None
    assert candidate.paise == 33_630  # ₹336.30
    assert candidate.title and "Dutiful Daughter" in candidate.title


def test_zero_results_is_a_miss_not_an_error(amazon_miss: str) -> None:
    """An ISBN nobody in India stocks must come back as None, which the caller
    turns into 'not sold in India' — a real signal, not a failure."""
    assert parse_amazon(amazon_miss) is None


def test_sponsored_results_are_ignored() -> None:
    """A sponsored row is frequently a different book. Letting one set the
    baseline would compare the shop against something unrelated."""
    html = """
    <div data-component-type="s-search-result">
      <span class="puis-sponsored-label-text">Sponsored</span>
      <span class="a-price"><span class="a-offscreen">₹99.00</span></span>
    </div>
    <div data-component-type="s-search-result">
      <span class="a-price"><span class="a-offscreen">₹450.00</span></span>
    </div>
    """
    assert parse_amazon(html).paise == 45_000


def test_cheapest_organic_result_wins() -> None:
    html = """
    <div data-component-type="s-search-result">
      <span class="a-price"><span class="a-offscreen">₹800.00</span></span>
    </div>
    <div data-component-type="s-search-result">
      <span class="a-price"><span class="a-offscreen">₹340.50</span></span>
    </div>
    """
    assert parse_amazon(html).paise == 34_050


def test_implausible_prices_are_rejected() -> None:
    """Shipping lines and discount badges must not become the baseline."""
    html = """
    <div data-component-type="s-search-result">
      <span class="a-price"><span class="a-offscreen">₹3.00</span></span>
    </div>
    """
    assert parse_amazon(html) is None


# --- generic retailer parsing -------------------------------------------------


def test_prefers_schema_org_offer_over_scraped_text() -> None:
    html = """
    <script type="application/ld+json">
    {"@type":"Product","name":"A Book",
     "offers":{"price":"499.00","priceCurrency":"INR"}}
    </script>
    <p>Was ₹1,299 — save big! Shipping ₹40</p>
    """
    assert parse_retailer(html).paise == 49_900


def test_ignores_non_inr_offers() -> None:
    html = """
    <script type="application/ld+json">
    {"@type":"Product","offers":{"price":"12.00","priceCurrency":"USD"}}
    </script>
    """
    assert parse_retailer(html) is None


def test_loose_page_text_is_never_treated_as_a_price() -> None:
    """The central regression. Scanning page text produced Rs 500 for every
    book on Bookswagon — a promotional banner ("FLAT 10% OFF (Up to Rs 500)")
    sitting above the product on every page. Taking the smallest figure instead
    picks shipping charges. No aggregate rescues an unstructured scan, so
    scanning is gone: no product-scoped markup means no price."""
    banner = """
    <div class="offer-item">App Exclusive Offer - FLAT 10% OFF (Up to ₹500)</div>
    <div><span>Delivery ₹40</span></div>
    """
    assert parse_retailer(banner) is None


def test_out_of_stock_listing_is_rejected() -> None:
    """Bookswagon returned the right book as an out-of-stock "International"
    edition at Rs 12,211. An unavailable listing is not an alternative the
    buyer has, and using it would make the shop look 98% cheaper."""
    html = """
    <script type="application/ld+json">
    {"@type":"Product","name":"Memoirs of a Dutiful Daughter",
     "offers":{"price":"12211.00","priceCurrency":"INR",
               "availability":"https://schema.org/OutOfStock"}}
    </script>
    """
    candidate = parse_retailer(html)
    assert candidate is not None
    assert candidate.in_stock is False


def test_in_stock_jsonld_offer_is_accepted() -> None:
    html = """
    <script type="application/ld+json">
    {"@type":"Product","name":"Some Book",
     "offers":{"price":"499.00","priceCurrency":"INR",
               "availability":"https://schema.org/InStock"}}
    </script>
    """
    candidate = parse_retailer(html)
    assert candidate.paise == 49_900
    assert candidate.in_stock is True


# --- soft-block detection -----------------------------------------------------


def _response(status: int, body: bytes) -> httpx.Response:
    return httpx.Response(status, content=body, request=httpx.Request("GET", "https://x"))


def test_small_amazon_response_is_treated_as_a_block() -> None:
    """Amazon answers a request with insufficient headers using a ~2KB stub
    containing no results. Parsed naively it looks like 'not sold in India' —
    that exact stub is why Amazon was written off as bot-walled during
    planning."""
    stub = _response(200, b"x" * 2_235)
    assert _is_soft_block(stub, min_bytes=20_000) is True


def test_full_amazon_response_is_not_a_block() -> None:
    real = _response(200, b"x" * 130_000)
    assert _is_soft_block(real, min_bytes=20_000) is False


def test_size_check_does_not_apply_to_json_hosts() -> None:
    """A small JSON body is perfectly normal; only HTML hosts get a floor."""
    assert _is_soft_block(_response(200, b'{"ok":true}'), min_bytes=None) is False


# --- the tiered chain ---------------------------------------------------------


class FakeClient:
    """Returns canned bodies by URL substring; None means blocked/unreachable."""

    def __init__(self, responses: dict[str, str | None]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    async def get(self, url: str, **kwargs: object):
        params = kwargs.get("params") or {}
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
        self.requested.append(full)
        for fragment, body in self.responses.items():
            if fragment in url:
                if body is None:
                    return None
                return httpx.Response(
                    200, text=body, request=httpx.Request("GET", full)
                )
        return None

    def degraded_hosts(self) -> list[str]:
        return []


_OFFER = (
    '<script type="application/ld+json">'
    '{"@type":"Product","offers":{"price":"%s","priceCurrency":"INR"}}</script>'
)
_AMAZON = (
    '<div data-component-type="s-search-result">'
    '<span class="a-price"><span class="a-offscreen">₹%s</span></span></div>'
)


async def test_amazon_wins_when_available() -> None:
    client = FakeClient({"amazon.in": _AMAZON % "336.30", "bookswagon": _OFFER % "500.00"})

    result = await fetch_indian_price(client, "9780140020304")

    assert result.price_paise == 33_630
    assert result.source == "amazon.in"
    assert result.available_in_india is True


async def test_falls_through_to_retailers_when_amazon_is_blocked() -> None:
    """Amazon returns HTTP 503 under sustained volume — confirmed in testing.
    The whole point of the tiers is that a block does not end the lookup."""
    client = FakeClient({"amazon.in": None, "bookswagon": _OFFER % "500.00"})

    result = await fetch_indian_price(client, "9780140020304")

    assert result.price_paise == 50_000
    assert result.source == "bookswagon"
    assert result.available_in_india is True
    assert result.attempts["amazon"] == "blocked or unreachable"


async def test_genuine_miss_everywhere_means_not_sold_in_india() -> None:
    """Empty pages from every tier: a real, useful answer."""
    client = FakeClient(
        {"amazon.in": "<html></html>", "bookswagon": "<html></html>",
         "bookstohome": "<html></html>", "thebookx": "<html></html>"}
    )

    result = await fetch_indian_price(client, "9780571166800", sources=("amazon", "retailers"))

    assert result.available_in_india is False
    assert result.unknown is False


async def test_everything_blocked_means_unknown_not_unavailable() -> None:
    """The critical distinction. Both outcomes leave available_in_india False,
    but scoring a network failure as scarcity would reward the failure."""
    client = FakeClient({"amazon.in": None, "bookswagon": None,
                         "bookstohome": None, "thebookx": None})

    result = await fetch_indian_price(client, "9780140020304", sources=("amazon", "retailers"))

    assert result.available_in_india is False
    assert result.unknown is True


async def test_sources_config_is_respected() -> None:
    client = FakeClient({"amazon.in": _AMAZON % "336.30", "bookswagon": _OFFER % "500.00"})

    result = await fetch_indian_price(client, "9780140020304", sources=("retailers",))

    assert result.source == "bookswagon"
    assert not any("amazon" in u for u in client.requested)


# --- the identity and stock guards -------------------------------------------


_JSONLD = (
    '<script type="application/ld+json">'
    '{"@type":"Product","name":"%s","offers":{"price":"%s","priceCurrency":"INR",'
    '"availability":"https://schema.org/InStock"}}</script>'
)


async def test_price_for_the_wrong_book_is_rejected() -> None:
    """A price without an identity check is meaningless — it would compare the
    shop against a different book entirely."""
    client = FakeClient({"amazon.in": None,
                         "thebookx": _JSONLD % ("A Completely Different Book", "499.00")})

    result = await fetch_indian_price(
        client, "9780140020304", title="Memoirs of a Dutiful Daughter",
        author="Simone de Beauvoir", sources=("retailers",),
    )

    assert result.price_paise is None
    assert result.attempts["thebookx"]["result"] == "title mismatch"


async def test_matching_title_is_accepted() -> None:
    client = FakeClient({"thebookx": _JSONLD % ("Memoirs of a Dutiful Daughter", "499.00")})

    result = await fetch_indian_price(
        client, "9780140020304", title="Memoirs of a Dutiful Daughter",
        author="Simone de Beauvoir", sources=("retailers",),
    )

    assert result.price_paise == 49_900
    assert result.available_in_india is True


async def test_out_of_stock_listing_does_not_set_the_baseline() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Memoirs of a Dutiful Daughter",'
        '"offers":{"price":"12211.00","priceCurrency":"INR",'
        '"availability":"https://schema.org/OutOfStock"}}</script>'
    )
    client = FakeClient({"thebookx": html})

    result = await fetch_indian_price(
        client, "9780140020304", title="Memoirs of a Dutiful Daughter",
        sources=("retailers",),
    )

    assert result.price_paise is None
    assert result.attempts["thebookx"] == "listing out of stock"


async def test_banner_only_page_yields_no_price() -> None:
    """End-to-end version of the Rs 500 banner regression."""
    banner = '<div class="offer-item">FLAT 10% OFF (Up to ₹500)</div>'
    client = FakeClient({"thebookx": banner, "bookswagon": banner, "bookstohome": banner})

    result = await fetch_indian_price(
        client, "9780140020304", title="Memoirs of a Dutiful Daughter",
        sources=("retailers",),
    )

    assert result.price_paise is None
    assert result.unknown is False        # pages loaded fine; there was just no price
    assert result.available_in_india is False
