# 25. On-demand dashboard and lean daemon runtime

Status: accepted

## Context

Running two persistent Python runtimes (`pooks.service` and `pooks-web.service`) on low-power devices (such as an Intel N100/N150 mini-PC or small VPS) consumed ~150–180 MB of resident memory (RSS) continuously, even when idle:

- `pooks-web` ran 24/7 in the background despite browsing occurring only occasionally. On every HTTP request to `/` or `/api/books`, it re-queried all in-stock rows (~634) and re-parsed thousands of JSON blobs for scores, tags, and categories, causing unnecessary database I/O and heap churn.
- `pooks daemon` imported `python-telegram-bot` at startup, carrying a ~26 MB RSS footprint and a 15-package dependency closure solely to make outbound HTTPS POST requests to Telegram's `sendMessage` endpoint.

## Decision

1. **On-demand dashboard via systemd socket activation and idle shutdown**:
   Systemd holds the listening port via `pooks-web.socket` (or `systemd.sockets.pooks-web` in NixOS). To ensure strictly on-demand activation, `pooks-web.service` does not declare `WantedBy=multi-user.target`, avoiding eager startup on boot or rebuild. `pooks serve` auto-detects inherited sockets (`fd=3` when `LISTEN_FDS` is present) and monitors request activity (`--idle-timeout 600`, defaulting to 10 minutes). After 10 minutes of inactivity, the server exits cleanly with status 0, dropping web dashboard memory to **0 MB RAM** while systemd keeps the socket open for future requests.
2. **In-memory catalogue caching via `PRAGMA data_version`**:
   `_load_books` caches the parsed catalogue in memory, checking SQLite's `PRAGMA data_version` (~0.01 ms). If unchanged, the cached index is reused directly, avoiding repeated SQL execution, JSON deserialization, and heap allocation. Sliced page books are shallow-copied so presentation payload attachments (`_attach_blurbs`, `_attach_sources`) never mutate cached entries.
3. **Lean Telegram client using native `httpx`**:
   `python-telegram-bot` is removed from dependencies and replaced with minimal dataclasses (`LinkPreviewOptions`, `InlineKeyboardButton`, `InlineKeyboardMarkup`) and direct `httpx` POST calls over TLS/HTTPS to Telegram's Bot API.
4. **Post-tick memory reclamation**:
   Scheduler ticks execute `gc.collect()` at the end of batch processing to return unreferenced cyclical objects to the heap.

## Consequences

- Continuous idle system memory drops from ~150–180 MB to ~38–40 MB (a ~75% reduction), with the web dashboard consuming 0 MB for the vast majority of the day.
- Dashboard response times improve and memory allocations on active requests are drastically reduced.
- Deletes `python-telegram-bot` from `pyproject.toml` and `nix/package.nix`, shrinking packaging closures while preserving 100% of existing notification functionality, HTML markup, buttons, covers, and HTTPS security.

See [`../design.md#the-dashboard-runs-on-demand-and-caches-the-in-stock-catalogue`](../design.md#the-dashboard-runs-on-demand-and-caches-the-in-stock-catalogue) for design details.
