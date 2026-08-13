# 13. Arrival dates are backfilled on the sweep, and do not converge

Status: accepted

## Context

The Store API omits creation timestamps entirely, so `date_created` comes from
`wp/v2` separately. `ingest.backfill_dates` existed but was reachable only from
`pooks sweep --with-dates`, so a daemon-run install never called it: 0 of 634
rows on the live database had an arrival date, and the dashboard had nothing to
show, sort or filter on.

Putting it on the five-minute idle tick looked right and was not. Nothing ever
deletes from `products` — a sweep only flips `in_stock` — and `wp/v2/product`
returns published posts only, so a delisted listing is an id that will never
resolve. It stays NULL and is re-selected every run: the work does not converge.
Worse, `product_id` is `INTEGER PRIMARY KEY`, i.e. the rowid, so an unordered
`LIMIT` returned the *lowest* ids — on live data 810, 864, 969, while the books
the dashboard ranks are 233188, 233180. The oldest rows are also the least likely
to resolve, so a batch of them could block the arrivals the feature exists for,
permanently.

## Decision

The backfill runs from the **hourly sweep**, on the client that pass already has
open. Selection is `WHERE date_created IS NULL AND in_stock = 1 ORDER BY
product_id DESC` — newest first, buyable only, which is the same rule
`improvable_books` applies: an unbuyable book cannot reach the digest, so a
request spent on it is traffic for nothing.

It is not claimed to converge. The hourly cadence is what makes re-asking
unresolvable ids affordable.

## Consequences

A cold catalogue takes a few hourly sweeps to acquire dates rather than a few
five-minute ticks. Sold-out listings never get an arrival date, so anything built
on date-of-arrival for price history has to fall back to `first_seen_at`, which
means something different — when *we* first saw it, not when the shop listed it —
and must be labelled distinctly wherever both are shown.

Dates are not load-bearing for detection (product ids are monotonic), so a
failure fetching them is logged and swallowed rather than allowed to take the
sweep's sold-out detection down with it.
