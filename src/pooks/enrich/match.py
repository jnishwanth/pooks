"""The matching ladder: cheap deterministic checks before any LLM call.

Roughly 93% of in-stock listings carry an ISBN and resolve directly. The rest
are matched on title and author, which needs verification — a title-based lookup
can quietly return a different book, and an unverified match poisons both the
rating and the blurb.

Order is deliberate: ISBN, then fuzzy string similarity, and only then an LLM
adjudication for the residual few percent. The residual is real; the shop lists
"The Archeology of Knowledge" (misspelled), which no exact match would find.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz

from pooks.models import slugify, strip_title

log = logging.getLogger(__name__)


class MatchMethod(StrEnum):
    ISBN = "isbn"
    FUZZY = "fuzzy"
    LLM = "llm"
    UNRESOLVED = "unresolved"


@dataclass
class MatchVerdict:
    accepted: bool
    method: MatchMethod
    score: float | None = None
    needs_adjudication: bool = False
    reason: str | None = None


def verify(
    *,
    query_title: str,
    query_author: str | None,
    candidate_title: str | None,
    candidate_author: str | None,
    accept_score: float = 92.0,
    reject_score: float = 70.0,
) -> MatchVerdict:
    """Decide whether a title/author lookup returned the right book.

    Three outcomes: confidently right, confidently wrong, or ambiguous — the
    last is the only case worth spending an LLM call on.
    """
    if not candidate_title:
        return MatchVerdict(False, MatchMethod.UNRESOLVED, reason="no candidate title")

    title_score = _similarity(strip_title(query_title), strip_title(candidate_title))

    # Author agreement is strong corroboration, so it can rescue a title whose
    # wording differs (subtitles, "and"/"&", edition notes).
    author_score = None
    if query_author and candidate_author:
        author_score = _similarity(query_author, candidate_author)

    combined = title_score
    if author_score is not None:
        combined = 0.7 * title_score + 0.3 * author_score

    if combined >= accept_score:
        return MatchVerdict(True, MatchMethod.FUZZY, score=combined)
    if combined < reject_score:
        return MatchVerdict(
            False, MatchMethod.UNRESOLVED, score=combined, reason="below reject threshold"
        )
    return MatchVerdict(
        False,
        MatchMethod.FUZZY,
        score=combined,
        needs_adjudication=True,
        reason="ambiguous; needs LLM adjudication",
    )


def _similarity(left: str, right: str) -> float:
    """Token-set ratio over normalised words.

    Token-set is the right choice because shop titles carry extra tokens
    (edition, format, subtitle) that a plain ratio would penalise: "The
    Archaeology of Knowledge" against "...and The Discourse on Language" should
    score highly, since one token set contains the other.

    The hyphen split matters — slugify joins words with '-', which would leave
    the whole title as a single token and silently reduce token_set_ratio to a
    plain character ratio.
    """
    return float(
        fuzz.token_set_ratio(slugify(left).replace("-", " "), slugify(right).replace("-", " "))
    )
