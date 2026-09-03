# 16. A rating is judged against its own category's distribution

Status: accepted

## Context

Manga and children's books dominated the top of the ranking. On the live
catalogue the top three in-stock books were all comics, and Naruto 30 scored
0.707 against 0.580 for *Memoirs of a Dutiful Daughter* — entirely on the
quality component, 4.4 from 8,574 ratings against 4.13 from 19,195.

Bayesian shrinkage cannot separate those: both samples are large, so both sit
essentially at their raw average. The defect is upstream of shrinkage — every
rating was judged against **one global mean of 3.9**, as though 4.4 meant the
same thing for a Naruto volume as for a literary memoir. It does not. Manga and
children's books are rated by people already invested in the series, so their
distributions sit structurally higher. Measured on this catalogue with `pooks calibrate --categories`, over 166 rated
in-stock books: Comics averages 4.248 against a global 3.9, +0.348 — the only
category that deviates enough to matter.

The measurement also corrected the premise. Children's books, suspected of the
same inflation, came out at 3.957 (+0.057) — essentially the global mean.
Fantasy (3.707) and Romance (3.659) sit *below* it.

ADR 14 separates the two complaints hiding in "too much manga at the top". This
records the half that is a scoring defect rather than a matter of taste.

## Decision

`quality_component` judges a rating against a per-category baseline. Both halves
move with it: the shrinkage prior, so a thin sample is pulled toward what its own
category averages, and the normalisation band, so the same rating reads
differently for different kinds of book.

Baselines live in `[ranking.category_baselines]`, keyed on the shop's own
category names — which cover the whole catalogue, where Hardcover tags reach
about three books in five. **The section ships empty, and empty means scoring is
byte-identical to having no categories at all.**

They are meant to be measured rather than chosen. `pooks calibrate --categories`
reports the observed mean and sample size per category and prints the rows worth
correcting in paste-ready TOML. A hand-set number here would be a taste penalty
wearing a measurement's clothes, and ADR 14 says where taste belongs instead.

A book in several categories is judged against the highest baseline it belongs
to: the most forgiving crowd it is a member of is the one whose ratings lifted
it.

## Consequences

With `Comics = 4.248` applied to the backfilled catalogue, the top six went from
containing three comics to Dune, A Man Called Ove, The Book Thief and two
listings of To Kill a Mockingbird; Calvin and Hobbes sits seventh, still high but
now competing on what its rating means for a comic.

Only Comics is configured. Baselining Fantasy and Romance would *raise* their
scores, since they rate below the mean — logically symmetric, and it lifts a
Sarah J Maas fantasy above To Kill a Mockingbird, which trades one version of
the original complaint for another. Symmetry is not on its own a reason.

The baseline is a property of the catalogue, so it drifts as stock turns over
and wants re-measuring occasionally. It is deliberately not derived at scoring
time: a self-tuning baseline would make a score depend on what else happened to
be in stock the day it was computed, and `pooks rescore` could then change a
ranking with no input having changed.

The band is applied as a *shift* from the global mean rather than by rebuilding
the floor around the baseline. That is not cosmetic: `3.9 - 0.9` is
`3.0000000000000004` and `0.9 + 0.7` is `1.5999999999999999`, so deriving the
band moved every score in its last bits even with the section empty — which the
live differential caught, and which is the difference between "no baselines
changes nothing" being true and being nearly true.
