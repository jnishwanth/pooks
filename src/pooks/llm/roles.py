"""The four LLM roles.

Every role is schema-validated, cached by (book_key, role, prompt_version), and
grounded in retrieved text rather than model memory wherever it can be. Bump
`[llm].prompt_version` in config.toml to invalidate a role's cache after
changing a prompt.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic import BaseModel, Field

from pooks.llm.client import LLMClient, LLMUnavailableError

log = logging.getLogger(__name__)


class Role(StrEnum):
    BLURB = "blurb"
    SPOILER_CHECK = "spoiler_check"
    RENOWN = "renown"
    MATCH = "match"
    EXTRACT = "extract"


# --------------------------------------------------------------------- blurb


class Blurb(BaseModel):
    blurb: str = Field(description="2-3 sentences, spoiler-free, describing what reading this "
                                   "book is like and who it suits")
    insufficient_context: bool = Field(
        default=False,
        description="True if the supplied material was too thin to describe the book "
                    "honestly. Say so rather than inventing content.",
    )


class SpoilerVerdict(BaseModel):
    has_spoilers: bool
    reason: str = Field(default="", description="What was revealed, if anything")


BLURB_SYSTEM = """You write short, spoiler-free notes about second-hand books for a \
reader deciding whether to buy.

Rules, in order of importance:

1. NO SPOILERS, ever. Do not reveal plot resolutions, twists, reveals, character \
deaths or fates, whether an ending is happy or sad, or any development a reader \
would want to discover themselves. For non-fiction, do not give away the central \
argument's conclusion or the outcome of the events described — describe the \
territory, not the destination.
2. Ground everything in the material provided below. Do not add facts from memory. \
If the material is too thin to say anything substantive, set insufficient_context \
to true and keep the blurb to what you can honestly support.
3. Describe the experience and the audience: tone, style, difficulty, what kind of \
reader would want it. That is what helps someone decide.
4. Be plain and concrete. No marketing language, no "a must-read", no "timeless \
classic". 2-3 sentences.
"""

SPOILER_CHECK_SYSTEM = """You check whether a short book description contains spoilers.

Flag has_spoilers=true if it reveals: plot resolution, twists, reveals, character \
deaths or fates, how a conflict is settled, the ending's emotional register, or — \
for non-fiction — the central conclusion or the outcome of the events covered.

Do NOT flag: premise, setup, setting, themes, tone, style, genre, author \
background, or anything a back cover would legitimately say.

Be strict. A false positive costs one regeneration; a false negative ruins a book \
for the reader.
"""


async def generate_blurb(
    client: LLMClient,
    *,
    title: str,
    author: str | None,
    synopsis: str | None,
    categories: list[str],
    rating: float | None,
    ratings_count: int | None,
    max_attempts: int = 2,
) -> tuple[Blurb, SpoilerVerdict | None]:
    """Generate a blurb, then verify it independently and regenerate if needed.

    The check is a separate call rather than a self-assessment in the same
    response: a model that just wrote a spoiler is poorly placed to notice it.
    """
    context = _blurb_context(title, author, synopsis, categories, rating, ratings_count)
    feedback = ""
    verdict: SpoilerVerdict | None = None

    for attempt in range(max_attempts):
        blurb = await client.structured(
            system=BLURB_SYSTEM + feedback,
            user=context,
            schema=Blurb,
        )

        verdict = await client.structured(
            system=SPOILER_CHECK_SYSTEM,
            user=f"Book: {title}\n\nDescription to check:\n{blurb.blurb}",
            schema=SpoilerVerdict,
        )

        if not verdict.has_spoilers:
            return blurb, verdict

        log.info(
            "blurb attempt %d for %r flagged as spoiler: %s",
            attempt + 1,
            title,
            verdict.reason,
        )
        feedback = (
            f"\n\nA previous attempt was rejected for spoilers: {verdict.reason}. "
            "Stay strictly at the level of premise, tone and audience."
        )

    # Exhausted attempts. Returning the flagged text would defeat the point.
    return (
        Blurb(
            blurb="No spoiler-free summary could be produced for this title.",
            insufficient_context=True,
        ),
        verdict,
    )


def _blurb_context(
    title: str,
    author: str | None,
    synopsis: str | None,
    categories: list[str],
    rating: float | None,
    ratings_count: int | None,
) -> str:
    parts = [f"Title: {title}"]
    if author:
        parts.append(f"Author: {author}")
    if categories:
        parts.append(f"Shop categories: {', '.join(categories)}")
    if rating and ratings_count:
        parts.append(f"Reader rating: {rating}/5 from {ratings_count:,} ratings")
    if synopsis:
        parts.append(f"\nPublisher synopsis (may itself contain spoilers — do not repeat "
                     f"any):\n{synopsis[:3000]}")
    else:
        parts.append("\nNo synopsis was available.")
    return "\n".join(parts)


# -------------------------------------------------------------------- renown


class RenownTier(StrEnum):
    CANONICAL = "canonical"
    MAJOR = "major"
    NOTABLE = "notable"
    STANDARD = "standard"
    UNKNOWN = "unknown"


class Renown(BaseModel):
    tier: RenownTier
    score: float | None = Field(
        default=None, ge=0.0, le=1.0, description="0-1 standing; null when abstaining"
    )
    abstained: bool = Field(
        default=False, description="True when there is not enough evidence to judge"
    )
    evidence: str = Field(default="", description="What the judgement rests on")


RENOWN_SYSTEM = """You judge how established a book is in its field — its standing, \
not its quality.

Tiers:
  canonical  widely taught or cited; a reference point in its field
  major      well known and influential, award-winning or genre-defining
  notable    respected, with a real readership beyond its niche
  standard   an ordinary trade book with no particular standing
  unknown    you cannot tell

ABSTAIN when unsure. Set abstained=true, tier=unknown and score=null. Inventing \
prestige for a book you do not recognise is far worse than admitting ignorance — \
the score feeds a ranking, and a confident wrong answer promotes the wrong book.

Weigh the supplied evidence (rating volume, publisher, publication year, \
categories) over your own recollection. Very high rating counts indicate reach; \
a scholarly press suggests standing in an academic field; neither is decisive \
alone. Keep evidence to one sentence.
"""


async def judge_renown(
    client: LLMClient,
    *,
    title: str,
    author: str | None,
    publisher: str | None,
    year: int | None,
    categories: list[str],
    rating: float | None,
    ratings_count: int | None,
) -> Renown:
    facts = [f"Title: {title}"]
    if author:
        facts.append(f"Author: {author}")
    if publisher:
        facts.append(f"Publisher: {publisher}")
    if year:
        facts.append(f"First published: {year}")
    if categories:
        facts.append(f"Categories: {', '.join(categories)}")
    if rating and ratings_count:
        facts.append(f"Rating: {rating}/5 from {ratings_count:,} ratings")
    else:
        facts.append("No rating data was found for this book.")

    try:
        renown = await client.structured(
            system=RENOWN_SYSTEM, user="\n".join(facts), schema=Renown
        )
    except LLMUnavailableError:
        return Renown(tier=RenownTier.UNKNOWN, abstained=True, evidence="LLM unavailable")

    # Enforce the abstention contract rather than trusting it.
    if renown.abstained or renown.tier is RenownTier.UNKNOWN:
        return Renown(
            tier=RenownTier.UNKNOWN,
            score=None,
            abstained=True,
            evidence=renown.evidence,
        )
    return renown


# --------------------------------------------------------------------- match


class MatchAdjudication(BaseModel):
    same_work: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(default="")


MATCH_SYSTEM = """You decide whether two book listings refer to the same work.

Treat as the SAME work: different editions, printings, translations or imprints; \
subtitle present or absent; minor spelling differences including misspellings; \
"Vol. 3" versus "3"; author name given in full, initials or a different \
transliteration.

Treat as DIFFERENT works: different books by the same author; a companion, sequel \
or prequel; an abridgement presented as a distinct work; a different author \
entirely.

Return confidence below 0.7 when genuinely unsure — an unverified match attaches \
the wrong book's rating and review to a listing.
"""


async def adjudicate_match(
    client: LLMClient,
    *,
    shop_title: str,
    shop_author: str | None,
    candidate_title: str,
    candidate_author: str | None,
) -> MatchAdjudication:
    user = (
        f"Listing A (from the shop):\n  title: {shop_title}\n"
        f"  author: {shop_author or 'unknown'}\n\n"
        f"Listing B (candidate match):\n  title: {candidate_title}\n"
        f"  author: {candidate_author or 'unknown'}"
    )
    try:
        return await client.structured(system=MATCH_SYSTEM, user=user, schema=MatchAdjudication)
    except LLMUnavailableError:
        return MatchAdjudication(
            same_work=False, confidence=0.0, reasoning="LLM unavailable; not matched"
        )


# ------------------------------------------------------------------- extract


class ExtractedBook(BaseModel):
    """Book identity read out of unstructured search results."""

    title: str | None = None
    author: str | None = None
    goodreads_url: str | None = None
    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    ratings_count: int | None = Field(default=None, ge=0)
    found: bool = False


EXTRACT_SYSTEM = """You read web search results and identify the book being described.

Extract only what is actually present in the results. Never infer a rating or a \
rating count that is not written there — a fabricated rating silently corrupts the \
ranking. Set found=false if the results do not clearly identify one specific book.

Prefer a goodreads.com/book/show/... URL when one appears.
"""


async def extract_from_search(
    client: LLMClient, *, query_title: str, query_author: str | None, results_text: str
) -> ExtractedBook:
    """Last-resort identification for listings with no ISBN.

    Structured data (Goodreads and AbeBooks both publish schema.org JSON-LD) is
    the primary path and needs no model. This exists for the residual case where
    there is no ISBN to look up and search results are all we have.
    """
    user = (
        f"Looking for: {query_title}"
        + (f" by {query_author}" if query_author else "")
        + f"\n\nSearch results:\n{results_text[:4000]}"
    )
    try:
        return await client.structured(system=EXTRACT_SYSTEM, user=user, schema=ExtractedBook)
    except LLMUnavailableError:
        return ExtractedBook(found=False)
