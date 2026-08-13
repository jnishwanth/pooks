# 15. Every source's answer is recorded, and the shown record is derived

Status: accepted

## Context

`enrichment` held one merged row per book and discarded every losing answer.
Three things followed from that, all of them costs:

- The record could not say *who* was asked. "Hardcover has no tags for this
  book" and "Hardcover was never asked" both stored NULL until `tags_json` was
  given an explicit `{}`, and that fix had to be made once per field.
- A repair could not improve one field without risking another, because the only
  way to write anything was to rewrite the whole row.
- Keeping a refetch from *downgrading* a record needed a hand-written per-field
  merge — `pipeline.merge` — and being hand-written it had to be got right once
  per field, forever.

The last is not hypothetical. A repair runs precisely when the previous attempt
was degraded, so the source may still be throttled and the new answer can be
worse than the one being replaced.

## Decision

A new `observations` table holds one row per `(book_key, field, source)`: what
each source said, kept. Re-asking a source replaces that source's row and
nothing else, so no source can overwrite another's answer.

`enrich/observations.py` derives the shown record from the set by walking the
configured ladder — `[ratings].chain` with its per-source floors for ratings,
`quality.PRICE_TIERS` for prices. That is monotonic by construction: adding a
row to a set can only move the winner up the ladder, never down.

Recording costs no extra requests. The chain still short-circuits; this only
stops the answers it already obtained being thrown away.

## Consequences

Facts that were previously unrepresentable now survive. Open Library's 5.0 from
a single rating — observed on live data — is kept as a fact about the book while
never being ranked on, because the floor is applied when choosing rather than
when storing. A thin or rejected answer no longer has to be re-fetched to be
re-examined.

`enrichment` remains the row every consumer reads — the scorer, the dashboard,
the digest, `improvable_books` — so nothing downstream changed. It is being
demoted to a projection of this table rather than the record of truth, and
`pipeline.merge` is what that demotion deletes.

The table grows with sources asked rather than with books, so a repair pass that
keeps chasing primary sources adds rows for books it has already seen. That is
the point, and at a few rows per book over a 633-book catalogue it is not a size
anyone needs to manage.
