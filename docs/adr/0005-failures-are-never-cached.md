# 5. Failures are never cached

Status: accepted

## Context

The same mistake was made in both caches. Enrichment cached a rating of "none"
that came from a blocked source; the LLM layer cached an empty blurb produced by
a rate-limited call. Both look like legitimate answers on read-back, and both
were permanent — the LLM one escapable only by bumping `prompt_version`, which
discards every role for every book. Eight books were found pinned to a blank
blurb this way.

## Decision

A result is cached only when it is an *answer*. An empty blurb is retried rather
than stored. A renown abstention is kept when the model genuinely could not tell
and discarded when the call never completed — `Renown.unavailable` exists solely
to tell those apart. On the enrichment side, `provenance.degraded_hosts` records
that a source never really answered.

## Consequences

Every layer that can fail needs a way to distinguish "no" from "we never found
out", which is why several types carry a third state (`IndianPrice.unknown`,
`hardcover._fetch_edition`'s `answered` flag, `tags_json` NULL vs `{}`).
Collapsing any of those pairs re-introduces this bug in that layer.
