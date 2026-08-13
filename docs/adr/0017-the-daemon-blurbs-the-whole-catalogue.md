# 17. The daemon writes blurbs for the whole catalogue, slowly and worst-last

Status: accepted

## Context

Blurbs only ever existed for books someone had asked for. `pooks blurbs --top N`
means *the top N books* — deliberately, so a second run is a no-op rather than a
walk deeper into the ranking — and cold-start inference is suppressed by design,
so after a backfill the catalogue is ranked and almost entirely undescribed. A
book at rank 60 could sit without a blurb indefinitely, even though the
dashboard shows it.

Doing it in bulk is not an option. The free LLM tier answers eventually but
often needs several attempts: a probe took six before one clean response, and
each retry backs off exponentially. A large batch would spend the whole tick in
backoff, and the daemon's ticks share a lock with the poll.

## Decision

The daemon writes `[schedule].blurbs_per_tick` blurbs on each idle tick — two by
default — selecting best-ranked-first from the books that lack one. `pooks
blurbs` and the daemon share one selection (`run.blurb_candidates`) and differ
only in how deep they scan: the command is bounded to the top N it was asked
for, the daemon is unbounded and takes a few per tick.

Blurbs come last in the idle tick, behind the repair pass. A blurb written from
a thin record has to be regenerated once the record improves, and the only way
to regenerate one is to bump `prompt_version` — which discards every cached role
for every book.

Books with no synopsis are counted rather than attempted, and the count is
logged. They are not waiting on an LLM call; they are waiting on the repair pass
finding them some retrieved text (ADR 10).

## Consequences

The top of the ranking is described within a day and the tail over the following
week, without anyone running a command. The cost is bounded by construction and
tunable to zero.

Blurb coverage now depends on the repair pass having run first, which is the
intended ordering but does mean a cold install produces very few blurbs until
enrichment has settled. That is visible in the log line rather than silent: it
reports how many books still need one and how many of those have nothing to
ground it.
