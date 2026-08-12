"""Enrichment robustness: throttle detection, cache lifetimes, matching."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from pooks.config import load_config
from pooks.enrich.abebooks import _parse_offers
from pooks.enrich.goodreads import _parse as parse_goodreads
from pooks.enrich.hardcover import _auth_header
from pooks.enrich.hardcover import _to_rating as _to_hardcover_rating
from pooks.enrich.http import _is_soft_block
from pooks.enrich.match import MatchMethod, verify
from pooks.enrich.pipeline import TTL_DEGRADED, _expiry_for
from pooks.enrich.sources import BookFacts, IndianPrice, RatingResult, flatten_tags_json

CHAIN = load_config().ratings["chain"]


def _response(status: int, body: bytes = b"") -> httpx.Response:
    return httpx.Response(status_code=status, content=body, request=httpx.Request("GET", "https://x"))


# --- soft-block detection -----------------------------------------------------


def test_detects_cloudfront_202_soft_block() -> None:
    """Goodreads answers a disliked client with 202 and an empty body. Treating
    that as success yields a silent 'no rating', which is worse than an error
    because it looks like a real miss and gets cached."""
    assert _is_soft_block(_response(202, b"")) is True


def test_202_with_content_is_not_a_block() -> None:
    assert _is_soft_block(_response(202, b"<html>real content</html>")) is False


@pytest.mark.parametrize("status", [429, 503])
def test_detects_explicit_rate_limits(status: int) -> None:
    assert _is_soft_block(_response(status, b"whatever")) is True


def test_normal_responses_are_not_blocks() -> None:
    assert _is_soft_block(_response(200, b"<html>ok</html>")) is False
    assert _is_soft_block(_response(404, b"not found")) is False


# --- cache lifetime -----------------------------------------------------------


def test_complete_primary_record_is_cached_indefinitely() -> None:
    """Only a record that is complete AND from primary sources is permanent."""
    facts = BookFacts(
        book_key="isbn:1",
        rating=4.1,
        ratings_count=7516,
        rating_source="goodreads",
        indian_price=IndianPrice(price_paise=33_630, source="amazon.in", available_in_india=True),
    )
    assert _expiry_for(facts, CHAIN) is None


def test_rating_without_a_price_is_not_permanent() -> None:
    """The defect this replaced: expiry keyed on `has_rating` alone, so a book
    enriched while Amazon was throttled kept an empty price forever. Five of
    nine rows in the first real database were frozen exactly this way."""
    facts = BookFacts(book_key="isbn:1", rating=4.1, ratings_count=7516,
                      rating_source="goodreads")
    assert _expiry_for(facts, CHAIN) is not None


def test_blocked_price_expires_soon_even_with_a_good_rating() -> None:
    facts = BookFacts(
        book_key="isbn:1", rating=4.1, ratings_count=7516, rating_source="goodreads",
        indian_price=IndianPrice(available_in_india=False, unknown=True),
    )
    expiry = _expiry_for(facts, CHAIN)
    assert expiry is not None
    assert datetime.fromisoformat(expiry) - datetime.now(UTC) <= TTL_DEGRADED


def test_fallback_source_is_revisited_sooner_than_a_genuine_miss() -> None:
    """A rating from Open Library is real but noisy — worth upgrading to
    Goodreads later, so it must not be cached as permanently as a primary one."""
    fallback = _expiry_for(
        BookFacts(book_key="isbn:1", rating=3.6, ratings_count=90,
                  rating_source="open_library"),
        CHAIN,
    )
    miss = _expiry_for(BookFacts(book_key="isbn:2", provenance={"attempts": {}}), CHAIN)
    assert fallback is not None and miss is not None
    assert fallback < miss


def test_blocked_lookup_expires_soon() -> None:
    """One throttling episode must not permanently mark books as unrated."""
    facts = BookFacts(book_key="isbn:1", provenance={"degraded_hosts": ["www.goodreads.com"]})
    expiry = _expiry_for(facts, CHAIN)
    assert expiry is not None
    assert datetime.fromisoformat(expiry) - datetime.now(UTC) <= TTL_DEGRADED


def test_exhausted_attempts_back_off_hard() -> None:
    """A book nobody has rated must stop consuming third-party traffic."""
    facts = BookFacts(book_key="isbn:1", provenance={"degraded_hosts": ["x"]})
    normal = _expiry_for(facts, CHAIN, attempts=1)
    exhausted = _expiry_for(facts, CHAIN, attempts=5)
    assert exhausted > normal


# --- rating usability ---------------------------------------------------------


def test_thin_rating_is_rejected() -> None:
    """Open Library reported 2.33 from 3 ratings for a work Goodreads rates
    4.11 from 7,516. Without the floor, whichever source answers first wins."""
    thin = RatingResult(source="open_library", rating=2.33, ratings_count=3)
    assert thin.is_usable(min_ratings_count=50) is False

    solid = RatingResult(source="goodreads", rating=4.11, ratings_count=7516)
    assert solid.is_usable(min_ratings_count=50) is True


# --- matching ladder ----------------------------------------------------------


def test_misspelled_shop_title_escalates_rather_than_guessing() -> None:
    """The shop really does list 'The Archeology of Knowledge' (misspelled),
    against a canonical title carrying an extra subtitle. String similarity
    alone cannot settle that, so the ladder must escalate rather than attach a
    possibly-wrong book's rating. In practice this title has an ISBN and never
    reaches the fuzzy stage; the ~7% without one do."""
    verdict = verify(
        query_title="The Archeology of Knowledge by Michel Foucault",
        query_author="Michel Foucault",
        candidate_title="The Archaeology of Knowledge and The Discourse on Language",
        candidate_author="Michel Foucault",
    )
    assert verdict.accepted is False
    assert verdict.ambiguous is True


def test_token_set_matching_survives_edition_wording() -> None:
    """Regression: slugify joins words with '-', so comparing slugs directly
    left each title as a single token and silently reduced token_set_ratio to a
    plain character ratio. 'Naruto 30' vs 'Naruto, Vol. 30' must match."""
    verdict = verify(
        query_title="Naruto 30 by Masashi Kishimoto",
        query_author="Masashi Kishimoto",
        candidate_title="Naruto, Vol. 30",
        candidate_author="Masashi Kishimoto",
    )
    assert verdict.accepted is True
    assert verdict.method is MatchMethod.FUZZY


def test_different_book_is_rejected() -> None:
    verdict = verify(
        query_title="Dirty Tricks by Michael Dibdin",
        query_author="Michael Dibdin",
        candidate_title="A History of Cambodia",
        candidate_author="David Chandler",
    )
    assert verdict.accepted is False
    assert verdict.ambiguous is False


def test_ambiguous_match_is_flagged_rather_than_guessed() -> None:
    verdict = verify(
        query_title="Plays",
        query_author="Alexander Ostrovsky",
        candidate_title="Plays: Volume Two",
        candidate_author="A. N. Ostrovsky",
        accept_score=99.0,
        reject_score=40.0,
    )
    assert verdict.accepted is False
    assert verdict.ambiguous is True


# --- structured-data parsing --------------------------------------------------


def test_parses_goodreads_aggregate_rating() -> None:
    html = """
    <script type="application/ld+json">
    {"@type":"Book","name":"A History of Cambodia",
     "author":[{"@type":"Person","name":"David P. Chandler"}],
     "aggregateRating":{"@type":"AggregateRating","ratingValue":3.77,"ratingCount":337}}
    </script>
    """
    result = parse_goodreads(html)
    assert result is not None
    assert result.rating == 3.77
    assert result.ratings_count == 337
    assert result.author == "David P. Chandler"


def test_goodreads_parse_returns_none_without_rating() -> None:
    assert parse_goodreads("<html>no structured data</html>") is None


def test_hardcover_auth_header_tolerates_a_prefixed_key() -> None:
    """Hardcover's dashboard hands you the token with `Bearer ` already on it.
    Prepending it again yields `Bearer Bearer eyJ...`, which the API rejects
    with "Malformed Authorization header" — observed against the live API."""
    assert _auth_header("eyJhbGc.abc.def") == "Bearer eyJhbGc.abc.def"
    assert _auth_header("Bearer eyJhbGc.abc.def") == "Bearer eyJhbGc.abc.def"
    assert _auth_header("  bearer eyJhbGc.abc.def  ") == "Bearer eyJhbGc.abc.def"


def test_hardcover_edition_maps_to_a_rating_and_nothing_else() -> None:
    """An edition becomes a rating and nothing more.

    The query used to request the edition's `pages` and the book's `slug`; the
    first filled a `RatingResult.pages` nothing read, the second built a
    Hardcover URL nothing read. Both were paid for on every lookup and
    discarded, and neither field survives on the result.
    """
    edition = {
        "book": {
            "title": "Memoirs of a Dutiful Daughter",
            "rating": 4.063492063492063,
            "ratings_count": 63,
            "description": "The first volume of her autobiography.",
            "contributions": [{"author": {"name": "Simone de Beauvoir"}}],
        }
    }
    result = _to_hardcover_rating(edition)
    assert result is not None
    # Rounded on construction: Hardcover returns a raw computed average.
    assert result.rating == 4.06
    assert result.ratings_count == 63
    assert result.author == "Simone de Beauvoir"
    assert result.synopsis == "The first volume of her autobiography."
    assert not hasattr(result, "pages")
    assert not hasattr(result, "url")


def test_hardcover_rating_needs_a_rating_count() -> None:
    """A book Hardcover lists but nobody has rated is not a rating."""
    assert _to_hardcover_rating({"book": {"title": "T", "rating": 5.0}}) is None
    assert (
        _to_hardcover_rating({"book": {"title": "T", "rating": 5.0, "ratings_count": 0}})
        is None
    )


def test_parses_abebooks_offers_and_splits_condition() -> None:
    html = """
    <script type="application/ld+json">
    {"@type":"ItemList","itemListElement":[
      {"item":{"@type":"Book","name":"A","offers":{"price":"5.28","priceCurrency":"USD",
        "itemCondition":"https://schema.org/UsedCondition"}}},
      {"item":{"@type":"Book","name":"B","offers":{"price":"18.66","priceCurrency":"USD",
        "itemCondition":"https://schema.org/NewCondition"}}}
    ]}
    </script>
    """
    offers = _parse_offers(html)
    assert len(offers) == 2
    # The used/new split drives the in-print flag. Prices are deliberately not
    # retained: they are USD, and comparing Indian prices against them rated
    # every book 87-91% cheaper regardless of what it was.
    assert sum(1 for o in offers if o.used) == 1
    assert not hasattr(offers[0], "price_usd")


def test_goodreads_non_redirect_is_ambiguous_not_a_confirmed_miss() -> None:
    """Under load Goodreads stops honouring the ISBN redirect and serves a plain
    search page, which is indistinguishable from "unknown ISBN" — except the
    same ISBN resolves fine minutes later. Observed: three ISBNs returned
    ratings on one run and "no match" on the next.

    Caching is why it matters. A confirmed miss is held for 30 days; mistaking a
    throttle for one marks a book unrated for a month on a temporary rate limit.
    """
    facts = BookFacts(
        book_key="isbn:1", provenance={"degraded_hosts": ["www.goodreads.com"]}
    )
    expiry = _expiry_for(facts, CHAIN)
    assert expiry is not None

    from datetime import UTC, datetime

    assert datetime.fromisoformat(expiry) - datetime.now(UTC) <= TTL_DEGRADED


# --- tag flattening -----------------------------------------------------------
#
# Three display paths show these tags — the digest via `BookFacts.flat_tags`,
# the dashboard and `pooks top` via the stored JSON column. They must agree.


def test_flat_tags_keeps_facet_order_and_dedupes() -> None:
    raw = '{"genre": ["fiction", "war"], "mood": ["tense"], "tags": ["war"]}'
    assert flatten_tags_json(raw) == ["fiction", "war", "tense"]


def test_flat_tags_handles_absent_and_empty() -> None:
    assert flatten_tags_json(None) == []
    assert flatten_tags_json("{}") == []
    assert flatten_tags_json("not json") == []


def test_the_json_and_facts_tag_paths_agree() -> None:
    """The invariant the three separate copies of this had already broken."""
    tags = {
        "genre": ["history", "war"],
        "mood": ["bleak"],
        "tags": ["war", "poland"],
        "content_warning": ["violence"],
    }
    assert flatten_tags_json(json.dumps(tags)) == BookFacts(book_key="k", tags=tags).flat_tags
