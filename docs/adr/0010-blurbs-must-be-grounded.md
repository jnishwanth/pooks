# 10. A blurb is only generated when retrieved text grounds it

Status: accepted

## Context

With no synopsis the model pads with metadata the digest card already shows —
*"categorized as history and non-fiction. With a 3.77/5 rating from 337
readers"*. That is worse than no blurb and costs a call.

## Decision

Generation is skipped outright when there is nothing to work from, and the skip
is recorded as a cached answer so it is not retried blindly. That made synopsis
coverage the real constraint, so Open Library backfills a description after
Google Books: it needs no ISBN — and the books without one are exactly those most
likely to lack a description — and a free-text lookup finds a better-populated
record than an ISBN, which resolves to whichever work Open Library happens to
link. Coverage went from 6 of 12 to 8 of 8.

## Consequences

Blurb quality is bounded by retrieval, not by the model, which is the intended
trade. A spoiler check runs as a *separate* call rather than a self-assessment,
because a model that just wrote a spoiler is poorly placed to notice it. Free-text
Open Library matches are verified through the same fuzzy ladder as ratings —
a wrong description is worse than none.
