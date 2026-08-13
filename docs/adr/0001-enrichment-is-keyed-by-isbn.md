# 1. Enrichment is keyed by ISBN, not product id

Status: accepted

## Context

The shop relists the same titles constantly: a sold-out book reappears weeks
later under a *new* product id with the same ISBN. Keying enrichment by listing
would re-fetch every rating, price comp and LLM role for a book already known,
which is the dominant cost in the whole pipeline — Goodreads is paced at 60s per
request and Amazon at 90s.

## Decision

`models.make_book_key` is the cache key everywhere: `isbn:<isbn>` when the
listing carries one (~93% do), falling back to a `ta:<title>|<author>` slug when
it does not. `enrichment` and `llm_cache` are keyed by it; `products` is keyed by
listing and joins across.

## Consequences

A relist costs zero API and zero LLM calls, which is what keeps the catalogue
inside a free LLM tier. Two costs follow:

- Recovering a missing author for an ISBN-less book *changes its key* and
  strands the old row. The sweep calls `Store.prune_orphaned_enrichment` for
  exactly that; the book is re-enriched once under the better key.
- Two listings of the same title share one enrichment row, so counts over
  products and counts over books are not the same number. `rank/health.py`
  mixes both deliberately and says so.

Undoing this re-introduces per-listing fetches, which the third-party pacing
cannot absorb.
