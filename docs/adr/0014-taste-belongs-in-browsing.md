# 14. Taste belongs in browsing; only measurable bias belongs in the score

Status: accepted

## Context

Manga and children's books dominate the top of the ranking. On the live
catalogue Naruto 30 scores 0.707 against 0.580 for *Memoirs of a Dutiful
Daughter*, entirely on the quality component: 4.4 from 8,574 ratings against 4.13
from 19,195. Bayesian shrinkage cannot help, because both samples are large.

Two different complaints hide in "there is too much manga at the top", and they
have different answers:

- *I do not want to read manga.* That is taste. It is personal, it changes by
  the day, and baking it into a score makes the ranking mean something different
  for every reader.
- *A 4.4 manga volume is not as good a find as a 4.13 literary memoir.* That is
  a measurable claim about rating distributions differing by category, and it is
  a defect in the score.

## Decision

The dashboard carries the first: tag and category filters with exclusions, so a
reader narrows what they are shown without changing what anything is worth.
Shop categories do the heavy lifting because they cover the whole catalogue
where Hardcover tags reach roughly three books in five.

The score may carry the second, but only from measurement — a per-category
rating baseline derived from the catalogue, never a hand-set penalty on a genre
someone dislikes. With no baselines configured the scoring is unchanged.

## Consequences

The digest pushed to Telegram is not filtered by browsing preferences, so the
score-side half is the only thing that can affect it. That is the honest split:
if a category genuinely rates higher for reasons unrelated to being a better
buy, fixing the score is right; if the reader simply does not like it, a filter
is right and a score change would be dishonest.

It also means the ranking cannot be tuned by adding categories to a blocklist.
Anyone tempted to should add a filter to their bookmark instead.
