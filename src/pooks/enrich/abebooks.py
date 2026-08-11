"""AbeBooks — scarcity and in-print detection.

This used to supply the price baseline, and that was wrong. AbeBooks quotes USD
from sellers shipping internationally, while Indian book prices sit structurally
far below US/UK ones. Every book therefore came out 87-91% cheaper locally,
whatever it was: a four-point spread across mass-market pop-psychology, manga
and a scarce literary memoir. The comparison had no discriminating power and
flattered junk exactly as much as a genuine find.

What remains is currency-independent and genuinely informative:

  * how many copies exist on the international used market (scarcity)
  * whether any are offered new, which is the only reliable in-print signal
    available from a free source

No price from this module reaches the score. India-facing prices come from
`enrich.indian_prices`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pooks.enrich.http import PoliteClient
from pooks.enrich.jsonld import as_float, extract_blocks, find_by_type, first_offer
from pooks.enrich.sources import ScarcitySignal

log = logging.getLogger(__name__)

SOURCE = "abebooks"
SEARCH_URL = "https://www.abebooks.com/servlet/SearchResults"


@dataclass
class _Offer:
    used: bool


async def fetch_scarcity(
    client: PoliteClient, isbn: str, *, max_listings: int = 20
) -> ScarcitySignal | None:
    url = f"{SEARCH_URL}?isbn={isbn}"
    response = await client.get(SEARCH_URL, params={"isbn": isbn})
    if response is None:
        return None

    offers = _parse_offers(response.text)[:max_listings]
    if not offers:
        log.debug("abebooks: no offers for %s", isbn)
        return ScarcitySignal(source=SOURCE, listing_count=0, url=url)

    return ScarcitySignal(
        source=SOURCE,
        listing_count=len(offers),
        has_new_offers=any(not offer.used for offer in offers),
        url=url,
    )


def _parse_offers(html: str) -> list[_Offer]:
    """Read the schema.org ItemList rather than the React-rendered markup.

    Prices are parsed only to confirm an entry is a real listing; the values are
    discarded.
    """
    offers: list[_Offer] = []

    for item_list in find_by_type(extract_blocks(html), "ItemList"):
        for entry in item_list.get("itemListElement") or []:
            book = entry.get("item", entry)
            if not isinstance(book, dict):
                continue
            offer = first_offer(book)
            if as_float(offer.get("price")) is None:
                continue
            condition = str(offer.get("itemCondition") or "").rsplit("/", 1)[-1]
            offers.append(_Offer(used=condition == "UsedCondition"))

    return offers
