# 20. The repair floor cannot ration the evidence the score is missing

Status: accepted

## Context

`[schedule].refresh_min_score` keeps a 90s Amazon lookup off books that cannot
clear the push threshold. Sound in the abstract, and measured on the live
catalogue it had locked the repair pass out of almost everything it exists for:

| | |
|---|---|
| in-stock books the repair pass would consider | **37** |
| ...of which took the expensive full-chain branch | **32** |
| in-stock books with no rating at all | **461** |
| ...of those, sitting below the floor | **461 — every one** |

The mechanism is circular. A book with no rating has no quality component, so
`confidence` shrinks its composite toward the neutral prior (ADR 9) and it scores
around 0.4. The floor then refuses it the chain — which is the only thing that
would give it a rating. It scores low *because* nobody has looked it up, and
nobody looks it up *because* it scores low.

This had already silently undone the previous commit. ADR 15's ledger was built
so the budget could go to books a source has genuinely never been asked about,
and `docs/design.md` recorded that as 163 books Goodreads has never seen, "so the
budget goes entirely to them". The floor was admitting **30** of those 163.

## Decision

The floor applies only to a book whose score already rests on a rating:

```sql
(s.score IS NULL OR s.score >= :min_score OR e.rating_source IS NULL) AS full_refresh_ok
```

A score computed with no rating is a statement about our ignorance, not about the
book, and it has no business rationing the lookup that would settle it.

**This is an exemption, not a removal.** The floor still refuses **135** in-stock
books that have a rating and score below it — the case it was written for, and
the one where the 90s is genuinely wasted. `tests/test_quality.py` pins both
halves, because either alone reads as the other.

Eligibility goes from 37 books to 497, of which 493 take the expensive branch.
That is ~14 hours of idle ticks, and it converges — the pass is bounded by
`MAX_REFRESH_ATTEMPTS` per book as it always was.

## Consequences

The daemon now spends its repair budget on every idle tick for the better part of
a day rather than exhausting 32 candidates and going quiet, which forces two
things that were previously masked.

**The tick needed a bound in seconds, not in books.** The two repair branches
differ by two orders of magnitude — one Hardcover call at a second against a chain
paced by Amazon's 90s — so `refresh_per_tick = 3` is anywhere from 3s to ~360s,
and it runs inside the lock `poll_tick` holds. Overrunning does not queue:
apscheduler skips the next poll with a warning nothing reads. The budget is
therefore derived from `poll_interval_s` rather than configured beside it, because
the interval is the thing it must not overrun and two numbers would drift. It
bounds *starts*, so a book already running always finishes.

**One failing book could stall the pass for ever.** The loop had no guard, and
the attempt counter that retires a hopeless book sat downstream of the raise — so
an exception meant the same book was picked first on every later tick and raised
again, indefinitely. Pre-existing, but this change multiplies the pool it can
happen in by fifteen, so the guard lands here: the exception is caught and the
attempt is still counted.

`pooks health` now reports several hundred improvable books where it reported
tens. That is the backlog becoming visible, not appearing.
