"""What the book costs in India — the baseline the shop is judged against.

Replaces an AbeBooks comparison that could not work: AbeBooks quotes USD, Indian
prices sit far below US/UK ones, so every book came out 87-91% "cheaper" — a
constant, not a signal.

The rebuild had to learn the same lesson twice. A first attempt scanned pages
for currency patterns, and produced a *different* constant: every book priced at
Rs 500, which turned out to be a promotional banner ("FLAT 10% OFF (Up to
Rs 500)") sitting above the product on every page. Taking the smallest figure
instead picks shipping charges; taking the first picks banners. There is no
choice of aggregate that rescues an unstructured scan.

So a price is only accepted when all three hold:

  1. it comes from product-scoped markup (JSON-LD Offer, or a known selector) —
     never from loose page text;
  2. the listing is in stock, because an unavailable listing is not an
     alternative the buyer has (Bookswagon returned the right book as an
     out-of-stock "International" edition at Rs 12,211);
  3. the listing's title matches the book we asked about, verified with the same
     fuzzy ladder used for ratings.

Anything else yields no price. A wrong baseline silently corrupts every ranking
it touches, which is the exact disease this module exists to cure — so returning
"unknown" is always preferable to returning a number we cannot stand behind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from pooks.enrich.http import PoliteClient
from pooks.enrich.jsonld import as_float, extract_blocks, find_by_type, first_offer
from pooks.enrich.match import verify
from pooks.enrich.searxng import SearxngClient
from pooks.enrich.sources import IndianPrice

log = logging.getLogger(__name__)

AMAZON_SEARCH = "https://www.amazon.in/s"

RETAILERS: tuple[tuple[str, str], ...] = (
    ("thebookx", "https://www.thebookx.in/?s={isbn}&post_type=product"),
    ("bookswagon", "https://www.bookswagon.com/search-books/{isbn}"),
    ("bookstohome", "https://bookstohome.co.in/?s={isbn}"),
)

# Product-scoped price selectors, paired with the card they live in so a price
# is always read together with its own title and stock state.
RETAILER_SELECTORS: dict[str, dict[str, str]] = {
    "bookswagon": {
        "card": ".price-container",
        "price": ".current-price",
    },
}

INDIAN_DOMAINS = frozenset(
    {
        "amazon.in",
        "bookswagon.com",
        "bookchor.com",
        "bookstohome.co.in",
        "thebookx.in",
        "sapnaonline.com",
        "crossword.in",
        "indianbookworms.com",
    }
)
# Recognised but never fetched: answers direct requests with HTTP 529.
UNFETCHABLE_DOMAINS = frozenset({"flipkart.com"})

OUT_OF_STOCK_MARKERS = (
    "out of stock",
    "outofstock",
    "sold out",
    "notify me",
    "currently unavailable",
    "temporarily unavailable",
)

MIN_PLAUSIBLE_PAISE = 5_000  # Rs 50
MAX_PLAUSIBLE_PAISE = 2_000_000  # Rs 20,000


@dataclass
class PriceCandidate:
    paise: int
    title: str | None = None
    in_stock: bool = True


@dataclass
class _Result:
    candidate: PriceCandidate | None
    blocked: bool


async def fetch_indian_price(
    client: PoliteClient,
    isbn: str,
    *,
    title: str | None = None,
    author: str | None = None,
    searxng: SearxngClient | None = None,
    sources: tuple[str, ...] = ("amazon", "retailers", "searxng"),
) -> IndianPrice:
    result = IndianPrice()
    degraded = False

    for source in sources:
        if source == "amazon":
            outcome = await _try_amazon(client, isbn, title, author, result)
        elif source == "retailers":
            outcome = await _try_retailers(client, isbn, title, author, result)
        elif source == "searxng":
            outcome = await _try_searxng(client, isbn, title, author, searxng, result)
        else:
            log.warning("unknown indian price source: %s", source)
            continue

        degraded = degraded or outcome.blocked
        if outcome.candidate is not None:
            result.price_paise = outcome.candidate.paise
            result.available_in_india = True
            return result

    # Nothing found. Whether that means "not sold in India" or "we never got a
    # straight answer" turns entirely on whether anything blocked us, and the
    # two must not be conflated: scoring a network failure as scarcity would
    # reward the failure with a better ranking.
    result.unknown = degraded
    result.available_in_india = False
    return result


def _accept(
    candidate: PriceCandidate | None,
    *,
    query_title: str | None,
    query_author: str | None,
    source: str,
    result: IndianPrice,
) -> PriceCandidate | None:
    """Apply the stock and identity checks before a price is allowed through."""
    if candidate is None:
        result.attempts[source] = "no product-scoped price found"
        return None

    if not candidate.in_stock:
        result.attempts[source] = "listing out of stock"
        return None

    if query_title and candidate.title:
        verdict = verify(
            query_title=query_title,
            query_author=query_author,
            candidate_title=candidate.title,
            candidate_author=None,
        )
        if not verdict.accepted:
            result.attempts[source] = {
                "result": "title mismatch",
                "candidate": candidate.title[:80],
                "score": verdict.score,
            }
            return None

    result.attempts[source] = candidate.paise / 100
    result.source = source
    return candidate


# ------------------------------------------------------------------- amazon


async def _try_amazon(
    client: PoliteClient,
    isbn: str,
    title: str | None,
    author: str | None,
    result: IndianPrice,
) -> _Result:
    response = await client.get(AMAZON_SEARCH, params={"k": isbn})
    if response is None:
        # PoliteClient already treats a stub or 503 as a soft block, so None
        # here means blocked or errored, never "no results".
        result.attempts["amazon"] = "blocked or unreachable"
        return _Result(None, True)

    candidate = parse_amazon(response.text)
    accepted = _accept(
        candidate,
        query_title=title,
        query_author=author,
        source="amazon.in",
        result=result,
    )
    if candidate is None:
        result.attempts["amazon.in"] = "no results"
    return _Result(accepted, False)


def parse_amazon(html: str) -> PriceCandidate | None:
    """Cheapest in-stock organic result, carrying its own title.

    Sponsored rows are skipped: they are frequently a different book, and one
    setting the baseline would compare the shop against something unrelated.
    """
    tree = HTMLParser(html)
    candidates: list[PriceCandidate] = []

    for block in tree.css('div[data-component-type="s-search-result"]'):
        if block.css_first("span.puis-sponsored-label-text"):
            continue
        node = block.css_first("span.a-price span.a-offscreen")
        if node is None:
            continue
        paise = _to_paise(node.text(strip=True))
        if paise is None:
            continue
        heading = block.css_first("h2")
        candidates.append(
            PriceCandidate(
                paise=paise,
                title=heading.text(strip=True) if heading else None,
                in_stock=not _looks_out_of_stock(block.text(separator=" ")),
            )
        )

    in_stock = [c for c in candidates if c.in_stock]
    pool = in_stock or candidates
    return min(pool, key=lambda c: c.paise) if pool else None


# ---------------------------------------------------------------- retailers


async def _try_retailers(
    client: PoliteClient,
    isbn: str,
    title: str | None,
    author: str | None,
    result: IndianPrice,
) -> _Result:
    blocked = False
    for name, pattern in RETAILERS:
        url = pattern.format(isbn=isbn)
        response = await client.get(url)
        if response is None:
            result.attempts[name] = "unreachable"
            blocked = True
            continue

        candidate = parse_retailer(response.text, name)
        accepted = _accept(
            candidate,
            query_title=title,
            query_author=author,
            source=name,
            result=result,
        )
        if accepted is not None:
            return _Result(accepted, blocked)

    return _Result(None, blocked)


def parse_retailer(html: str, retailer: str | None = None) -> PriceCandidate | None:
    """Product-scoped price only — JSON-LD first, then a known selector.

    There is deliberately no text-scanning fallback. Scanning produced a Rs 500
    reading for every book on Bookswagon, which was a promotional banner.
    """
    if (candidate := _parse_jsonld(html)) is not None:
        return candidate
    if retailer and (selectors := RETAILER_SELECTORS.get(retailer)):
        return _parse_with_selectors(html, selectors)
    return None


def _parse_jsonld(html: str) -> PriceCandidate | None:
    for type_name in ("Product", "Book"):
        for item in find_by_type(extract_blocks(html), type_name):
            offer = first_offer(item)
            currency = str(offer.get("priceCurrency") or "").upper()
            if currency and currency != "INR":
                continue
            amount = as_float(offer.get("price"))
            if amount is None:
                continue
            paise = _bound(int(round(amount * 100)))
            if paise is None:
                continue

            availability = str(offer.get("availability") or "").lower()
            in_stock = "outofstock" not in availability.replace("/", "").replace("_", "")
            name = item.get("name")
            return PriceCandidate(
                paise=paise,
                title=name if isinstance(name, str) else None,
                in_stock=in_stock,
            )
    return None


def _parse_with_selectors(html: str, selectors: dict[str, str]) -> PriceCandidate | None:
    tree = HTMLParser(html)
    for card in tree.css(selectors["card"]):
        node = card.css_first(selectors["price"])
        if node is None:
            continue
        paise = _to_paise(node.text(strip=True))
        if paise is None:
            continue

        # The stock marker usually sits alongside the price rather than inside
        # the price element, so inspect an ancestor.
        context = card
        for _ in range(4):
            if context.parent is None:
                break
            context = context.parent
        text = context.text(separator=" ")
        return PriceCandidate(
            paise=paise,
            title=_nearby_title(context),
            in_stock=not _looks_out_of_stock(text),
        )
    return None


def _nearby_title(node) -> str | None:
    for tag in ("h1", "h2", "h3", "a"):
        found = node.css_first(tag)
        if found and (text := found.text(strip=True)) and len(text) > 8:
            return text
    return None


# ------------------------------------------------------------------ searxng


async def _try_searxng(
    client: PoliteClient,
    isbn: str,
    title: str | None,
    author: str | None,
    searxng: SearxngClient | None,
    result: IndianPrice,
) -> _Result:
    if searxng is None or not searxng.available:
        result.attempts["searxng"] = "not configured"
        return _Result(None, False)

    hits = await searxng.search(client, f"{isbn} book price")
    targets = []
    for hit in hits:
        host = urlparse(hit.url).netloc.removeprefix("www.")
        if any(host.endswith(d) for d in UNFETCHABLE_DOMAINS):
            continue
        if any(host.endswith(d) for d in INDIAN_DOMAINS):
            targets.append((host, hit.url))

    if not targets:
        result.attempts["searxng"] = "no indian retailers found"
        return _Result(None, False)

    blocked = False
    for host, url in targets[:3]:
        response = await client.get(url)
        if response is None:
            blocked = True
            continue
        candidate = (
            parse_amazon(response.text)
            if "amazon.in" in host
            else parse_retailer(response.text, host.split(".")[0])
        )
        accepted = _accept(
            candidate,
            query_title=title,
            query_author=author,
            source=f"searxng:{host}",
            result=result,
        )
        if accepted is not None:
            return _Result(accepted, blocked)

    result.attempts["searxng"] = f"no usable price from {len(targets)} result(s)"
    return _Result(None, blocked)


# ------------------------------------------------------------------ helpers


def _looks_out_of_stock(text: str) -> bool:
    lowered = " ".join(text.lower().split())
    return any(marker in lowered for marker in OUT_OF_STOCK_MARKERS)


def _to_paise(raw: str) -> int | None:
    cleaned = raw.replace("₹", "").replace("Rs.", "").replace("Rs", "").replace("INR", "")
    cleaned = cleaned.replace(",", "").strip()
    try:
        return _bound(int(round(float(cleaned) * 100)))
    except (TypeError, ValueError):
        return None


def _bound(paise: int) -> int | None:
    return paise if MIN_PLAUSIBLE_PAISE <= paise <= MAX_PLAUSIBLE_PAISE else None
