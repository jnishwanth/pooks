# 11. Detection rests on Last-Modified, corroborated by two cheap signals

Status: accepted

## Context

The whole 5-minute poll rests on the header being a trustworthy change signal,
so it was verified rather than assumed. Over one window the header advanced
(10 Aug 19:25 → 11 Aug 17:37) and the in-stock total fell 634 → 633, while zero
products were created and the maximum in-stock id was unchanged. So it moves on a
pure stock change, not only on new listings — the case that mattered and could
not be confirmed at first.

## Decision

The poll sends a conditional GET and stops on 304. `changed` is derived from
three signals: the header, the `x-wp-total` in-stock count, and the maximum
product id. The hourly sweep reconciles independently and is the only place
sold-out detection is valid, since that works by absence.

## Consequences

The two fallback signals cost nothing — they are headers and ids already in the
response — and guard against the header's semantics changing rather than carrying
detection outright. Nothing needs to re-run the check by hand: every sweep that
finds real changes while the header has not advanced logs a warning, which is a
stronger test than sampling because it fires on actual stock movement.

The poll and the sweep must request the *same* window, since the poll's signals
are compared against what the last sweep stored. `store_api._in_stock_page` is
the single definition, and its ordering is load-bearing: the poll's max id is the
catalogue maximum only because page 1 of a newest-first list holds the newest ids.
