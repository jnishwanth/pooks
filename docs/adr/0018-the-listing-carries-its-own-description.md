# 18. The listing carries its own description, and it is not evidence

Status: accepted

## Context

The WooCommerce Store API returns a `description` for every product, and the
request sets no `_fields` filter — so it has been arriving in the body of every
poll and every sweep since the first commit, and `Product.from_store_api` has
been dropping it on the floor.

Measured against the live shop on 2026-09-03, over all 574 in-stock listings:
**570 carry one (99.3%)**, median 1,290 characters, min 89, max 2,543.
`short_description` is populated on 1. The text is real markup — paragraphs,
entities, and on 117 of the 574 the wrapper divs of the chat UI the shop pasted
it out of.

That matters because retrieval is the binding constraint on everything
downstream. ADR 10 refuses to write a blurb with nothing to ground it, and only
411 of 633 in-stock books had a synopsis — so a third of the catalogue was
undescribable, and enrichment was spending a Google Books call plus up to three
Open Library calls per book chasing text the shop had already handed us.

## Decision

The description is ingested into `products.description` as plain text, via the
selectolax parser the price scrapers already use. It costs no extra request.

It is deliberately **not** enrichment. It does not reach `enrichment.synopsis`,
`BookFacts`, `confidence` or any score. Two reasons, and the second is the one
that would otherwise get undone:

- It is not independent evidence about the book. It is copy the shop wrote to
  sell it, and `confidence` measures how much anyone else has said.
- Every listing has one. Folding it into the synopsis component would add the
  same 0.10 to all 630 books — the identical argument that retired
  `affordability`, which was 1.00 for nearly everything and separated nothing.

For the same reason it is absent from `ingest.diff.METADATA_FIELDS`: a reworded
description is not a change worth an event. Confirmed on a live sweep — 574
listings, 570 descriptions newly captured, 2 `METADATA_CHANGE` events.

## Consequences

Blurb grounding stops being bounded by third-party retrieval, which is what
ADR 10 named as the constraint and what ADR 17 reports as `ungrounded` on every
tick. The catalogue fills itself: the hourly sweep upserts every in-stock
listing, so one sweep takes coverage from 0 to 570 of 574 with no backfill pass
of its own.

Nothing in the ranking moves. A full `pooks rescore` over the live catalogue
produced 630 score rows byte-identical to the same rescore at the previous
commit, which is the property this ADR exists to keep true — anyone tempted to
"finally use" the description as a synopsis would silently reprice the entire
catalogue.

The column is assigned rather than COALESCEd on upsert, unlike `date_created`.
The dates are preserved because the Store API never carries them, so a sweep's
`None` would blank what wp/v2 taught us; the description arrives in the same
payload, so a shop that removes one is reporting a fact.
