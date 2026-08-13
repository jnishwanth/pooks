# 8. Rating floors are per-source

Status: accepted

## Context

What counts as a thin sample depends on how big the community is. A single global
floor of 50 was quietly costing coverage: Hardcover reported 21 ratings for a
book Goodreads rates from 19,193, and 21 is a respectable sample on a site that
size — but the shared floor discarded it and the book ended up unrated.

## Decision

Floors live in `[ratings.min_count_by_source]`, read through
`RatingResolver.floor_for`, which falls back to `[ratings].min_ratings_count`.

## Consequences

They can afford to be loose because Bayesian shrinkage is the real defence: a
rating backed by 15 votes is pulled hard toward the global mean, so accepting it
costs little while rejecting it costs the book its largest score component.

A TOML footgun comes with it: every plain key of `[ratings]` must appear **above**
the `[ratings.min_count_by_source]` header, or it is parsed into the subsection.
That silently emptied the rating chain once. `tests/test_config.py` guards it.
