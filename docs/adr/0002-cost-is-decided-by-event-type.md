# 2. Cost is decided by event type, declared as data

Status: accepted

## Context

Enrichment and inference are the expensive stages. The rule that motivates
everything else: a book going *out* of stock must update the database and stay
silent — no fetches, no LLM call, no push. Deciding that at each call site
produces a heuristic per site, and heuristics drift.

## Decision

`models.py` declares three frozensets — `ENRICH_EVENTS`, `INFERENCE_EVENTS`,
`NOTIFY_EVENTS` — and the diff persists the first two onto each event row as
`requires_enrichment` / `requires_inference`. `run.process_pending` does what the
row says rather than re-deriving it.

Notifiability cannot ride on the row, because a push also depends on a score that
does not exist at classification time. It has one predicate instead,
`models.notifiable`, paired with `rank.score.pushable` for the score half.

## Consequences

The cost policy is inspectable in SQL and testable without network. The split
across two mechanisms — two columns and two predicates — is the price, and it is
why both predicates carry docstrings naming their counterpart. A new event type
that is added to the enum but to none of the sets is silently free, which is the
correct default.
