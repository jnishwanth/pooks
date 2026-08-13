# 7. Tags come from Hardcover, or not at all

Status: accepted

## Context

Filtering by mood or genre keeps the ranking objective — taste applies when you
browse, not when the score is computed. The shop's own categories cannot carry
it: 24 exist, but *Literature & Fiction* (308) and *Non Fiction* (293) cover
nearly everything and 357 of 633 books have just one. Alternatives were surveyed
and none work: StoryGraph and LibraryThing return 403, BookWyrm has a bot wall,
BookBrainz knows only `workType`, and Open Library's subjects are a multilingual
folksonomy.

## Decision

Tags come from Hardcover's structured `cached_tags` in four facets, kept as its
own slugs so filters stay stable. Coverage is roughly 3 books in 5 and **the gaps
stay empty** — an LLM guessing genres produces tags indistinguishable from
sourced ones once they are chips in a filter, with no way to tell which is which.

The lookup is **unconditional**, not part of the rating chain: Hardcover sits
second there, so whenever Goodreads answers — the common case — it would never be
queried.

## Consequences

`{}` (asked, has none) and NULL (never asked) must stay distinct, including for
books with no ISBN, which cannot be looked up at all. Collapsing them marks ~40%
of the catalogue improvable forever and burns the repair budget on lookups that
can never succeed. The tags repair is also the one repair
`[schedule].refresh_min_score` does not ration, since that floor exists to keep a
90s Amazon lookup off unpushable books and tags are a browsing filter rather than
a scoring input.
