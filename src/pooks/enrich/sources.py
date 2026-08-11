"""Result types shared by every enrichment source."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RatingResult:
    """A rating from one source, with the provenance needed to display it."""

    source: str
    rating: float
    ratings_count: int
    title: str | None = None
    author: str | None = None
    url: str | None = None
    synopsis: str | None = None
    pages: int | None = None

    def is_usable(self, min_ratings_count: int) -> bool:
        """Whether this rating carries enough weight to be trusted.

        Thin samples are noise: recon found Open Library reporting 2.33 from 3
        ratings for a work Goodreads rates 4.11 from 7,516.

        The threshold is per-source, since what counts as thin depends on how
        big the community is — see `[ratings.min_count_by_source]`.
        """
        return self.rating > 0 and self.ratings_count >= min_ratings_count


@dataclass
class ScarcitySignal:
    """How hard a book is to find on the international used market.

    Derived from AbeBooks. Deliberately carries no prices: AbeBooks quotes USD,
    and Indian book prices sit structurally far below US/UK ones, so comparing
    against it made every book look 87-91% cheaper — a constant, not a signal.
    What survives is currency-independent and genuinely informative: how many
    copies exist worldwide, and whether any are new (the in-print signal).
    """

    source: str = "abebooks"
    listing_count: int = 0
    has_new_offers: bool = False
    url: str | None = None

    @property
    def has_data(self) -> bool:
        return self.listing_count > 0

    @property
    def scarcity(self) -> float:
        """0-1, where 1 means almost unobtainable. Saturates at 20 listings."""
        return max(0.0, 1.0 - (min(self.listing_count, 20) / 20.0))


@dataclass
class IndianPrice:
    """The cheapest price the book can be had for in India.

    This is the baseline the shop is judged against, because it is what the
    buyer would otherwise actually pay. `available_in_india=False` is a
    meaningful outcome rather than a failure: a book no Indian retailer stocks
    can only be imported, which makes a local second-hand copy genuinely
    valuable.

    `unknown` is the third state, and must stay distinct from the second: a
    throttled or errored lookup tells us nothing, and scoring it as scarce would
    reward a network failure.
    """

    price_paise: int | None = None
    source: str | None = None
    url: str | None = None
    available_in_india: bool = False
    unknown: bool = False
    attempts: dict[str, Any] = field(default_factory=dict)

    @property
    def has_price(self) -> bool:
        return self.price_paise is not None and self.price_paise > 0

    @property
    def price_inr(self) -> float | None:
        return None if self.price_paise is None else self.price_paise / 100


@dataclass
class BookFacts:
    """Everything enrichment learned about one book, before scoring."""

    book_key: str
    isbn: str | None = None
    resolved_title: str | None = None
    resolved_author: str | None = None
    rating: float | None = None
    ratings_count: int | None = None
    rating_source: str | None = None
    synopsis: str | None = None
    in_print: bool | None = None
    scarcity: ScarcitySignal | None = None
    indian_price: IndianPrice | None = None
    match_method: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def has_rating(self) -> bool:
        return self.rating is not None and self.ratings_count is not None
