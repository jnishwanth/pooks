# Design notes

Why pooks works the way it does. Every claim here was measured against the live
shop rather than assumed, and most of them record a version that did not work
before the one that did.

`README.md` is the user-facing guide; `docs/adr/` records the decisions
themselves in short form. This file holds the evidence behind them, in pipeline
order — detection, ingest, enrichment, pricing, caching, ranking, presentation.

## Detection

### `Last-Modified` is a trustworthy change signal

Verified rather than assumed, since the whole 5-minute poll rests on it. Over one
window the header advanced (10 Aug 19:25 → 11 Aug 17:37) and the in-stock total
fell 634 → 633, while **zero products were created** in that period and the
maximum in-stock id was unchanged. So it moves on a pure stock change, not only
on new listings — the case that mattered and could not be confirmed at first.

The in-stock total and maximum product id are still compared alongside it, and
the hourly sweep still reconciles independently. They now cost nothing and guard
against the header's semantics changing rather than carrying detection outright.

Nothing needs to re-run that check by hand: every sweep that finds real changes
while the header has not advanced logs a warning saying so, which is a stronger
test than sampling — it fires on actual stock movement rather than hoping some
occurs during the sample window.

## Ingest

### Half the catalogue has no author, and the title usually does

The shop leaves the `Author` attribute unset on ~51% of in-stock listings —
verified against the live API, which returns Book Condition, ISBN, Publisher and
Pages for such products and no author at all. (An early measurement of 99%
sampled only the 200 newest products; older inventory is not tagged.)

It is almost always in the title: 293 of 324 untagged books carry a
"... by <Author>" suffix, which `strip_title` already locates in order to remove
it. Harvesting it takes coverage from **49% to 95%**, measured on the live
catalogue. The remainder — *Sapiens*, *Homo Deus* — genuinely have no author in
the listing and fall back to whatever enrichment learned from the rating source.

> `book_key` for an ISBN-less book is derived from title and author, so
> recovering an author changes the key and strands its cached enrichment. The
> new key is better, so the sweep prunes orphaned rows and the book is
> re-enriched once. Books with an ISBN are unaffected.

## Enrichment and blocking

### Scraped sources lie about being blocked

Two hosts return a *successful-looking* response when they don't want to serve
you, and both parse as a legitimate "nothing found":

- **Goodreads** (AWS WAF) answers with **HTTP 202 and a zero-length body**, not
  429. Roughly ten requests in a few minutes triggers it; recovery takes about
  six minutes.
- **Amazon.in** answers a request with insufficient headers using a **200 and a
  ~2KB stub** containing no results. This is why the first version of this
  project wrongly recorded Amazon as permanently bot-walled and built the whole
  price leg around AbeBooks instead. A full browser header set (`Accept`,
  `Sec-Fetch-*`, `Upgrade-Insecure-Requests`) returns 130–620KB of real results.

`PoliteClient` therefore treats both shapes as soft blocks, applies a per-host
minimum plausible body size, and trips a circuit breaker rather than caching the
silence as a miss.

Mitigations: a 60s minimum interval, explicit 202-empty detection, a per-host
circuit breaker, and cache lifetimes that reflect answer quality — a rating never
expires, a genuine miss lasts 30 days, but a miss caused by a blocked source is
retried in 30 minutes.

At steady state (~15 arrivals/day) this costs about 15 minutes of wall clock a
day. A cold-start backfill of all 634 in-stock books takes roughly 10 hours; run
it once in the background.

### Amazon.in throttles under sustained volume, and coverage depends on it

Confirmed in testing: Amazon returns **HTTP 503** after enough cumulative
requests — 30s spacing was not enough, and once tripped it stays blocked for
several minutes. It recovers fully, so the interval is now 90s with jitter. At
~15 arrivals/day that costs ~20 minutes of wall clock; during a bulk backfill it
is the rate-limiting step.

Be realistic about the fallbacks: they are small shops with limited catalogues.
In testing, thebookx and bookstohome had no listing at all for most ISBNs, and
Bookswagon's one hit was an out-of-stock import. **When Amazon is blocked,
expect few or no Indian prices.** The tiers stop a block from being fatal; they
do not replace Amazon.

That is a deliberate trade. The alternative — relaxing the product-scoped,
in-stock and title-match checks to squeeze out more numbers — is what produced
a uniform ₹500 for every book. Missing prices are visible and harmless;
fabricated ones are neither.

The distinction that matters is preserved throughout: a blocked lookup is
recorded as `unknown` and cached for only 30 minutes, never as "not sold in
India". Only a genuine zero-results page earns the scarcity credit.

The same applies to ratings: when a source is blocked, provenance records
`blocked` rather than `no match`, so a throttling episode is not mistaken for a
gap in the catalogue.

## Pricing

### The baseline is the Indian price, not a foreign one

The first version compared against AbeBooks used prices plus estimated import
shipping. That could not work. Indian book prices sit structurally far below
US/UK ones, so the foreign comp beat the shop's price every single time:

| Book | Shop | AbeBooks landed | "Saving" |
|---|---|---|---|
| 8 Rules Of Love | ₹199 | ₹2,094 | 90.5% |
| Naruto Vol 29 | ₹250 | ₹2,150 | 88.4% |
| Memoirs of a Dutiful Daughter | ₹220 | ₹2,005 | 89.0% |
| Dragon Ball Z 05 | ₹250 | ₹2,794 | 91.1% |

Every book landed in an 87–91% band — a four-point spread covering mass-market
pop-psychology, manga and a scarce literary memoir alike. That is a constant, not
a signal, and it flattered junk exactly as much as a genuine find.

The baseline is now the cheapest price in India, because that is what you would
otherwise actually pay. AbeBooks keeps only what is currency-independent:
listing count (scarcity) and whether new copies exist (the in-print flag).

Three outcomes, and the last two must not be conflated:

- **price found** → `value` scales with the discount, nudged by scarcity
- **not sold in India** → mildly positive; importing is the only alternative
- **lookup blocked** → *unknown*, scored as missing. Scoring a network failure
  as scarcity would reward the failure with a better ranking.

### A price is only believed if it passes three checks

The rebuild reproduced the original bug before fixing it. A first attempt
scanned retailer pages for currency patterns and priced *every* book at ₹500 —
which turned out to be a promotional banner, "FLAT 10% OFF (Up to ₹500)",
sitting above the product on every page. Taking the smallest figure instead
picks shipping charges (₹40 delivery beat a ₹349 book). No choice of aggregate
rescues an unstructured scan.

Worse, Bookswagon returned the *right* book as an out-of-stock "International"
edition at ₹12,211 — a number that would have made the shop look 98% cheaper.

So a price is accepted only when all three hold:

1. **product-scoped markup** — JSON-LD `Offer` or a known per-retailer selector,
   never loose page text;
2. **in stock** — an unavailable listing is not an alternative the buyer has;
3. **title matches** — verified with the same fuzzy ladder used for ratings,
   because a price attached to a different book is worse than no price.

Failing any of them yields no price. A wrong baseline silently corrupts every
ranking it touches, which is the exact disease this module exists to cure.

## Caching

### Failures are never cached, anywhere

Two caches, and the same mistake made in both. Enrichment cached a rating of
"none" that came from a blocked source; the LLM layer cached an empty blurb
produced by a rate-limited call. Both looked like legitimate answers on read
back, and both were permanent — the LLM one escapable only by bumping
`prompt_version`, which discards every role for every book. Eight books were
found pinned to a blank blurb this way.

The rule now holds in both places: a result is only cached when it is an
*answer*. An empty blurb is retried. A renown abstention is kept when the model
genuinely could not tell and discarded when the call never completed — the
`unavailable` flag exists solely to tell those apart.

### Falling back is temporary, not permanent

Enrichment degrades when a source is throttled, and for a while that degradation
was forever: expiry was keyed on `has_rating` alone and never looked at the
price, so a book enriched during an Amazon outage kept an empty price
indefinitely, and a rating from Open Library was cached as durably as one from
Goodreads. Five of nine rows in the first real database were frozen that way.

Cache lifetime now reflects *how good* the answer was:

| State | Revisited |
|---|---|
| A source was throttled during enrichment | 30 min |
| Price lookup blocked | 30 min |
| Rating or price from a fallback tier | 3 days |
| Everything from primary sources | never |
| All sources answered, genuinely nothing found | 30 days |
| 5 unproductive refresh attempts | 30 days |

The daemon spends idle ticks on repairs, worst records first: blocked prices
before fallback sources, and within that by score, so the top of the ranking
becomes correct first. In-stock books only; an unbuyable book cannot reach the
digest, so upgrading it is traffic spent for nothing.

The hourly sweep additionally fills in the creation dates the Store API omits,
on the client it already has open — `wp/v2` answers a hundred ids to a request,
and `pooks sweep --with-dates` used to be the only caller, so a daemon-run
install never had an arrival date to show or sort by. This does **not**
converge: a delisted book is kept forever (a sweep only flips `in_stock`) and
wp/v2 answers for published posts only, so ids it will never resolve stay NULL
and are re-asked by the same bounded SELECT on every sweep. The hourly cadence
is what makes that acceptable; on the five-minute poll it would not be.

Refreshes are **monotonic**. A repair runs precisely when the last attempt was
degraded, so the source may still be throttled and the refetch can come back
*worse*. Writing that blindly would downgrade the record and re-mark it
improvable — a book could oscillate between tiers and be re-fetched forever. The
merge is per-field and keeps the better of old and new, so the two halves can
recover independently: a refresh can pick up the price while Goodreads is still
blocked.

### A merged row cannot say who was asked

Enrichment kept one row per book and threw the losing answers away, so the
record could not distinguish "Hardcover has nothing for this book" from
"Hardcover was never asked" — a distinction the repair pass turns on, and one
that had to be bolted back on per field (`tags_json` NULL versus `{}`).

`observations` now holds one row per (book, field, source). Re-asking a source
replaces that source's row and no other, and the record everything downstream
reads is derived from the set by walking the configured ladder. That makes a
refetch safe by construction: adding a row can only move the winner up the
ladder, which is what the hand-written per-field merge existed to guarantee.

It costs no extra requests — the chain still stops at the first usable answer,
and this only keeps what it already fetched. On live data that immediately
preserved facts the merged row could not hold, such as Open Library reporting
5.0 from a single rating: kept as a fact, never ranked on, and no longer
re-fetched to be re-rejected.

## Tags

#### A merged row cannot say who was asked

Enrichment kept one row per book and threw the losing answers away, so the
record could not distinguish "Hardcover has nothing for this book" from
"Hardcover was never asked" — a distinction the repair pass turns on, and one
that had to be bolted back on per field (`tags_json` NULL versus `{}`).

`observations` now holds one row per (book, field, source). Re-asking a source
replaces that source's row and no other, and the record everything downstream
reads is derived from the set by walking the configured ladder. That makes a
refetch safe by construction: adding a row can only move the winner up the
ladder, which is what the hand-written per-field merge existed to guarantee.

It costs no extra requests — the chain still stops at the first usable answer,
and this only keeps what it already fetched. On live data that immediately
preserved facts the merged row could not hold, such as Open Library reporting
5.0 from a single rating: kept as a fact, never ranked on, and no longer
re-fetched to be re-rejected.

## Tags come from Hardcover, or not at all

Filtering by mood or genre keeps the ranking objective — taste applies when you
browse, not when the score is computed. The shop's own categories cannot carry
it: 24 exist, but *Literature & Fiction* (308) and *Non Fiction* (293) cover
nearly everything and 357 of 633 books have just one.

Hardcover publishes structured `cached_tags` in four facets — Genre, Mood, Tag,
Content Warning — with its own slugs, which are kept verbatim so filters stay
stable. Alternatives were surveyed and none work: StoryGraph and LibraryThing
return 403, BookWyrm has a bot wall, BookBrainz knows only `workType`
(Novel/Poem), and Open Library's subjects are a multilingual folksonomy
(`Liebesbeziehung`, `Chang Pian Xiao Shuo`).

Coverage is roughly 3 books in 5, and **the gaps stay empty**. An LLM guessing
genres produces tags indistinguishable from sourced ones once they are chips in
a filter, and there would be no way to tell which is which.

> The lookup is **unconditional**, not part of the rating chain. Hardcover sits
> second there, so whenever Goodreads answers — the common case — it is never
> queried and no tags would arrive at all.
>
> `{}` (asked, has none) and NULL (never asked) are distinct, including for
> books with no ISBN, which cannot be looked up at all. Collapsing them would
> mark ~40% of the catalogue improvable forever and burn the repair budget on
> lookups that can never succeed.
>
> A NULL row is a repair candidate in its own right, since a book that is
> otherwise entirely from primary sources matches nothing else the repair pass
> selects on — but only while `HARDCOVER_API_KEY` is set, for the same reason.
> That repair asks Hardcover and nothing else: the rating and price are already
> primary by construction, so re-running the chain would spend Goodreads' 60s
> and Amazon's 90s on answers the merge is guaranteed to discard. It is also the
> one repair `[schedule].refresh_min_score` does not ration, since that floor
> exists to keep the 90s Amazon lookup off books that cannot be pushed — tags
> are a browsing filter rather than a scoring input, and gating them the same
> way left most of the catalogue untagged permanently.

## Ranking

### Rating floors are per-source

What counts as a thin sample depends on how big the community is. A single
global floor of 50 was quietly costing coverage: Hardcover reported 21 ratings
for a book Goodreads rates from 19,193, and 21 is a respectable sample on a site
that size — but the shared floor discarded it and the book ended up unrated.

Floors now live in `[ratings.min_count_by_source]`. They can afford to be loose
because Bayesian shrinkage is the real defence: a rating backed by 15 votes gets
pulled hard toward the global mean, so accepting it costs little, while
rejecting it costs the book its largest score component.

> TOML footgun: every plain key of `[ratings]` must appear **above**
> `[ratings.min_count_by_source]`. Anything after that header is parsed into the
> subsection — which silently emptied the rating chain once. `tests/test_config.py`
> guards it.

## Inference

### A blurb is only written when there is something to ground it

Blurbs come from retrieved text, not model memory. With no synopsis the model
pads with metadata the card already shows — *"categorized as history and
non-fiction. With a 3.77/5 rating from 337 readers"* — so generation is skipped
outright when there is nothing to work from. Better output, and a saved call.

That made synopsis coverage the real constraint, and it was 6 of 12. Open
Library now backfills it after Google Books, which matters for two reasons: it
needs no ISBN (and the books without one are exactly those most likely to lack
a description), and an ISBN resolves to whichever work record Open Library
happens to link, which is often a sparse stub. A free-text lookup finds the
better-populated record — verified against the same fuzzy ladder used for
ratings first, because a wrong description is worse than none. Coverage went to
8 of 8.

`pooks blurbs --top N` means *the top N books*, so a second run is a no-op
rather than quietly walking deeper into the ranking.

## Dashboard

### Searching the dashboard

`?q=` is a fuzzy match over title and author via `rapidfuzz` (already a
dependency, already used by the matching ladder), so `?q=beauvior` still finds
de Beauvoir. `min_rating` and `min_ratings_count` filter on the rating itself —
the latter being the one that makes a rating trustworthy, and the natural
companion to the Bayesian shrinkage in the scorer.

Filters run over the whole in-stock list rather than the current page, so a
narrow search still finds a book ranked 400th. A search also ignores the
score and confidence filters: someone typing an author's name wants to know
whether the shop has the book, not whether the pipeline has scored it yet.

### Every search from the form used to return 422

The filter form renders `value=""` for a numeric filter that is not set, so a
browser submitted `min_rating=&min_ratings_count=` alongside every search.
FastAPI cannot coerce `""` to a number and rejected the whole request with
`Input should be a valid number` — which reads as the *search box* wanting a
number rather than text. The filtering code it never reached was correct.

It is fixed at the endpoint rather than in the template, because a hand-typed
`?min_rating=` failed identically. The obvious repair — `float | None` with the
existing `ge` bound — is worse than the bug: pydantic raises *inside* the `ge`
validator when the value is None, trading the 422 for a 500. Each parameter
substitutes its own default for a blank instead, and genuinely invalid values
(`min_rating=9`, `limit=0`) are still rejected.

### Filtering is faceted, and exclusion is the useful half

Tags are grouped by Hardcover's own facets — genre, mood, tag, content warning —
because they are different questions, and forty chips in one undifferentiated
list is not a filter. `tag_mode=all` narrows to books carrying every selected
tag; `any` widens.

Shop categories are offered alongside them, and they are what actually works
today: Hardcover reaches roughly three books in five, the shop's own categories
reach all of them. So `exclude_category=Comics` is the filter that answers "less
manga, please" while tag coverage catches up.

Counts on each chip are computed *after* filtering, so the number is what
clicking it would leave rather than what the catalogue holds. An active tag is
listed even at a count of zero, and its facet is looked up from the whole
catalogue rather than the filtered set — otherwise a tag that filtered
everything out would take its own "unclick me" control with it.

Every link on the page is rebuilt from the complete filter state. Clicking a
genre used to drop the search that found it, because the link was a bare
`?tag=`.

### The arrival date is two different facts

`date_created` is when the shop listed the book; `first_seen_at` is when this
pipeline first saw it, which can be months later for existing shelf stock. The
card falls back to the second when the first is missing but labels it
differently, because treating them as one number would make a long-shelved book
look like a new arrival.

Sorting has to parse them rather than compare strings: `date_created` arrives
from wp/v2 naive and `first_seen_at` carries an offset, and Python refuses to
compare the two — so the catalogue could not be sorted by arrival at all while
the sweep was still filling dates. The `added_within_days` window excludes books
with no known date rather than assuming they are recent, since the window exists
to answer "what is new".
