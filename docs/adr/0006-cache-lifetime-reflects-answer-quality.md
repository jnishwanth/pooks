# 6. Cache lifetime reflects how good the answer was

Status: accepted

## Context

Expiry was originally keyed on `has_rating` alone and never looked at the price,
so a book enriched during an Amazon outage kept an empty price indefinitely, and
a rating from Open Library (documented as too noisy to rank on) was cached as
durably as one from Goodreads. Five of nine rows in the first real database were
frozen that way. Degrading gracefully is the design; degrading *permanently* is
the bug.

## Decision

`enrich/quality.py` derives a tier for each half of a record from what is already
stored, and `_expiry_for` sets a lifetime from it: 30 minutes when a source was
throttled or a price lookup was blocked, 3 days for a fallback tier, never for
primary sources, 30 days for a genuine miss or an exhausted retry budget. A
repair pass revisits the worst records first, and `MAX_REFRESH_ATTEMPTS` bounds
it.

Refreshes are **monotonic**: `pipeline.merge` keeps the better of old and new per
field, because a repair runs precisely when the source may still be throttled, so
a refetch can come back worse.

## Consequences

No extra bookkeeping is written at enrichment time — the tiers are read back out
of `rating_source` and `in_price_source`. The cost is that "how good is this
record" is inferred rather than recorded, which is what ADR 14 would revisit if
the observations table lands. Without the monotonic merge a record can oscillate
between tiers and be re-fetched forever.
