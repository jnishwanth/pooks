# 21. Tags strip Hardcover's community UUID suffixes

Status: accepted

## Context

Hardcover publishes structured `cached_tags` across Genre, Mood, Tag and
Content Warning. ADR 7 chose to keep Hardcover's own slugs so filters stay stable
without inventing a vocabulary of our own.

In practice, Hardcover allows community tag submissions and disambiguates
user-created tags by appending a 36-character UUID4 suffix
(`-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`). In the live
catalogue of 621 tagged books, this affected **100 books** (16%) across 104
distinct tags (e.g. `mafia-1e59fab6-82ef-49cd-9ebb-e877f8bad176`,
`classics-a6d38e19-11a4-42a5-8bd4-76960d21479d`,
`science-fiction-fantasy-4c14c349-8d52-4893-aaf0-34f7e33bf275`).

Keeping these suffixes verbatim broke the very stability ADR 7 existed to
provide:

1. **Broken filtering**: Filtering by `classics` in the dashboard or digest
   missed the 3 books tagged `classics-a6d38e19-...`.
2. **Duplicate tags on single books**: 8 books carried both the clean slug and
   the UUID slug for the exact same semantic concept (e.g.
   `science-fiction-fantasy` alongside
   `science-fiction-fantasy-4c14c349-...`).
3. **Leaked internal identifiers**: Raw hash strings leaked into user-facing
   filter chips and Telegram cards (`mafia 1e59fab6 82ef...`).

## Decision

Tag slugs strip any trailing UUID4 pattern
(`-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`), and tags
within each facet are deduplicated preserving original insertion order.

Defense-in-depth is applied across three points, matching the pattern established
for `round_rating`:

1. **Ingestion**: `hardcover.fetch_tags` normalises slugs before returning them to
   the pipeline.
2. **Read**: `sources.parse_tags_json` normalises slugs on the way out, so any
   un-migrated row or cache payload returns clean tags to callers.
3. **Storage**: `db.store._DATA_MIGRATIONS` auto-repairs `enrichment.tags_json`
   and `observations.value_json` on connect. The probe uses SQLite `GLOB` for
   sub-millisecond execution, ensuring clean databases take no write lock.

## Consequences

Hardcover remains the single tag authority and its taxonomy is preserved, while
eliminating community-tag fragmentation and duplicate chips.

Existing catalogues auto-migrate without re-fetching all 621 books over
Hardcover's rate-limited API. Dashboard filters and facet counts now group
cleanly across the entire catalogue.
