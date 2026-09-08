# 24. Dashboard defers presentation payloads and connects without per-request migrations

Status: accepted

## Context

Every request to `/` and `/api/books` in `pooks serve` previously called `_load_books`,
which fetched the full in-stock catalogue (`ranked_in_stock`, ~634 books), queried
and deserialized blurbs from `llm_cache` for every book, and queried the `observations`
table for every book, deserializing thousands of JSON records.

Neither blurbs nor observation source ledgers are read by filtering (`q`, `tags`,
`categories`, `min_rating`, `added_within_days`), sorting, or facet counting.
Loading them for the entire catalogue on every request meant hundreds of redundant
database reads and JSON parses just to render a 100-book page.

Additionally, `_open()` executed `connect()`, which re-read `schema.sql`, executed
`conn.executescript()`, and executed 10+ migration probes per HTTP request, while
failing to close the SQLite connection and leaving unclosed connection warnings.

## Decision

1. **Decouple catalogue indexing from presentation payloads**:
   `_load_books` loads only the lightweight catalogue index (`products`, `scores`,
   and basic `enrichment` fields) used for in-memory filtering and facet calculation.
   Blurbs (`_attach_blurbs`) and observation source ledgers (`_attach_sources`) are
   attached strictly to the paginated slice being rendered (`offset : offset + limit`).
2. **Fast per-request read connections without DDL probes**:
   `connect` supports `migrate=False`, skipping `executescript` and migration probes
   for dashboard requests. Full schema initialization and migrations run once on app
   startup (lifespan).
3. **Clean connection lifecycle**:
   Dashboard request handlers wrap connection usage in `try...finally` to ensure
   `store.conn.close()` is always executed.

## Consequences

- Dashboard request latency and memory allocations are significantly reduced: for a
  standard 100-book page, ~84% of blurb queries, observation queries, and JSON
  deserializations are avoided.
- Resource warnings from unclosed SQLite connections are eliminated.
- Single-transaction rescoring (`rescore_in_stock`) turns hundreds of disk fsyncs into
  one atomic commit.
- API and browse outputs remain byte-for-byte identical.

See [`../design.md`](../design.md#dashboard) for design details.
