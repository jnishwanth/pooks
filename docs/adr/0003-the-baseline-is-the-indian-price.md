# 3. The price baseline is the Indian price, not a foreign one

Status: accepted

## Context

The first version scored value by comparing the shop against AbeBooks plus
estimated import shipping. Indian book prices sit structurally far below US/UK
ones, so the foreign comp won every single time: measured across mass-market
pop-psychology, manga and a scarce literary memoir, every book landed in an
87–91% "saving" band. A four-point spread across that range is a constant, not a
signal, and it flattered junk exactly as much as a genuine find.

## Decision

Value is measured against the cheapest price the book can be had for *in India*,
because that is what the buyer would otherwise actually pay. AbeBooks is retained
for what is currency-independent and nothing else: listing count (scarcity) and
whether new copies exist (the in-print flag). No figure from AbeBooks reaches the
score.

## Consequences

`enrichment` deliberately stores no AbeBooks price, and `[prices]` documents
that. Coverage now depends on Amazon.in, which throttles — so missing prices are
common and are scored as *missing* rather than guessed. Three outcomes must stay
distinct: price found, not sold in India (mildly positive), and lookup blocked
(unknown, scored as absent). Collapsing the last two rewards a network failure
with a better ranking.

See [`../design.md`](../design.md#pricing) for the measurements.
