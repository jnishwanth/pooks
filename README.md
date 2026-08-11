# pooks

Watches [oldbookdepot.in](https://oldbookdepot.in) for newly in-stock books,
enriches each with real rating data and used-market price comps, ranks them, and
pushes a spoiler-free digest to Telegram plus a local dashboard.

Built to run on an Intel N150 NUC. Single process, SQLite, ~150MB RSS.

## Quick start

```bash
uv sync
cp .env.example .env      # then fill in the keys below
uv run pooks sweep        # seed the catalogue (~634 in-stock books)
uv run pooks process      # enrich, infer, score
uv run pooks top          # see the ranking
uv run pooks serve        # dashboard on :8080
```

`.env` keys, in order of how much they matter:

| Key | Effect if missing |
|---|---|
| `OPENROUTER_API_KEY` | **No blurbs and no renown scoring.** Everything else works. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | No push; the dashboard still works. |
| `SEARXNG_URL` | The ~9% of listings without an ISBN get no rating. |
| `GOOGLE_BOOKS_API_KEY` | Weaker synopsis coverage. Unauthenticated access is permanently quota-exhausted (HTTP 429), so this is effectively required for that leg. |
| `HARDCOVER_API_KEY` | One fewer fallback in the rating chain. |

To use a local model instead of OpenRouter, set `[llm].provider = "ollama"` in
`config.toml`. Both speak the OpenAI protocol, so nothing else changes.

## How it works

```
every 5 min ──► poll (conditional GET; 304 → stop, no body)
                  │
                  ▼
                diff ──► classify events
                  │
     ┌────────────┼─────────────┐
     ▼            ▼             ▼
NEW_IN_STOCK  PRICE_CHANGE   SOLD_OUT
BACK_IN_STOCK      │       METADATA_CHANGE
     │             │             │
     └──────┬──────┘             ▼
            ▼              update DB, stay silent
     enrich (cached by ISBN)
            │
            ▼
     LLM roles (cached by book + prompt version)
            │
            ▼
          rank ──► Telegram (score ≥ threshold)
               └─► dashboard
```

Cost is controlled by event type, not by heuristics at call sites:

| Event | Enrich | Infer | Notify |
|---|---|---|---|
| `NEW_IN_STOCK` | yes | yes | if above threshold |
| `BACK_IN_STOCK` | cache hit | no | if above threshold |
| `PRICE_CHANGE` | prices | no | no |
| `SOLD_OUT` | no | no | no |
| `METADATA_CHANGE` | no | no | no |

Enrichment is keyed by **ISBN, not product id**. The shop relists the same
titles constantly, so a book costs API and LLM calls exactly once, ever.

## Commands

| Command | Purpose |
|---|---|
| `pooks poll` | One cheap conditional-GET change check |
| `pooks sweep` | Full in-stock sweep; the only valid place for sold-out detection |
| `pooks enrich --limit N` | Fetch ratings and comps |
| `pooks process --dry-run` | Enrich, infer and score pending events |
| `pooks rescore` | Recompute scores from cache after tuning weights — no network, no LLM |
| `pooks top` | Ranked in-stock list |
| `pooks calibrate` | Score distribution + what each threshold would actually push |
| `pooks notify --dry-run` | Render the digest without sending |
| `pooks probe-llm` | Verify the configured provider actually works |
| `pooks verify-polling` | Check whether `Last-Modified` is a trustworthy signal |
| `pooks serve` / `pooks daemon` | Dashboard / scheduler |

## Ranking

```
score = (0.45·quality + 0.25·renown + 0.20·value + 0.10·affordability)
        shrunk toward a neutral prior by confidence, × condition factor
```

All weights live in `config.toml`; `pooks rescore` applies changes for free.

Two mechanisms do the heavy lifting, both there because the naive version failed
on real data:

**Bayesian shrinkage on ratings.** Rating counts span three orders of magnitude,
and thin samples are noise — Open Library reported 2.33 from 3 ratings for a work
Goodreads rates 4.11 from 7,516. Shrinking toward the global mean in proportion
to sample size stops a 5.0-from-3 outranking a 4.2-from-20,000.

**Confidence shrinkage on the composite.** Dropping missing components and
renormalising the remaining weights sounds right, but a book with no rating, no
renown and no comps still has an affordability score — dividing by that single
weight handed it 0.95 and put it top of the ranking. The composite is now shrunk
toward a neutral prior in proportion to the evidence behind it, so unknown books
sit in the middle rather than winning.

## Data sources

| Source | Used for | Notes |
|---|---|---|
| WooCommerce Store API | Catalogue | Public JSON. No HTML scraping. |
| Goodreads | Ratings (primary) | schema.org `aggregateRating`, work-level. `/search?q=<isbn>` redirects to the book page. |
| Hardcover | Ratings, synopsis | Free key. Paste it verbatim — it already includes the `Bearer ` prefix. |
| Google Books | Synopsis, ratings | Key effectively required. |
| Open Library | Popularity | Ratings too sparse to rank on; used as a proxy only. |
| Amazon.in | **Indian price (primary)** | Organic search results only. |
| Bookswagon / bookstohome / thebookx | Indian price (fallback) | Direct ISBN search URLs. |
| SearXNG | Indian price (last resort), ISBN-less lookup | Your own instance. |
| AbeBooks | Scarcity, in-print | schema.org `ItemList`. **No prices** — see below. |

Flipkart is recognised but never fetched: it answers direct requests with HTTP
529. Amazon's Product Advertising API is unusable — deprecated in May 2026 and
closed to new applicants — so the price comes from the public search page.

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

## Deploying

### NixOS (flake + module)

```nix
inputs.pooks = {
  url = "github:jnishwanth/pooks";
  inputs.nixpkgs.follows = "nixpkgs";   # required; see below
};

# in your nixosSystem modules:
inputs.pooks.nixosModules.default

services.pooks = {
  enable = true;
  environmentFile = "/var/lib/pooks/secrets.env";  # keys, outside the store
  settingsFile = "${inputs.pooks}/config.toml";
  serve.port = 3004;
};
```

`nix/goji-example.nix` has the full integration for a host that fronts services
with Caddy, including the secrets file and the first-run commands.

`inputs.nixpkgs.follows` is required. The module resolves its package through
the *consumer's* `pkgs`, so a second nixpkgs input is downloaded and evaluated
for nothing.

Two environment variables exist for packaged installs, because the source tree
is read-only in the Nix store: `POOKS_CONFIG` and `POOKS_DATA_DIR`
(plus `POOKS_SERVE_HOST` / `POOKS_SERVE_PORT`). The module sets them; a
development checkout ignores them and uses paths beside the source.

The package runs the full test suite at build time — every test is offline, so
a broken build is a real signal.

**Do not pin a Python version in `nix/package.nix`.** It tracks nixpkgs' default
`python3` deliberately. Hydra only builds the default package set at scale, so
pinning to a non-default set makes every dependency a cache miss and builds it
from source — which drags in fastapi's *test-only* closure
(`inline-snapshot → isort → pylama → vulture → pint → uncertainties → scipy`)
and fails the whole `nixos-rebuild` on a flaky test in scipy's own suite. That
happened, with the package pinned to `python312Packages`. Deployment parity
comes from the nixpkgs revision, not from a version number.

### Anything else (systemd + venv)

```bash
sudo mkdir -p /opt/pooks && sudo chown $USER /opt/pooks
rsync -a --exclude .venv --exclude data ./ /opt/pooks/
cd /opt/pooks && uv sync
sudo cp deploy/pooks*.service /etc/systemd/system/
sudo systemctl enable --now pooks@$USER pooks-web@$USER
```

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

### Push thresholds

`push_score_threshold` and `push_min_confidence` started as guesses. Run
`pooks calibrate` to see the real distribution, what the current settings would
push, and what each percentile cut-off implies:

```
current settings (score >= 0.62, conf >= 0.5) would push 4 of 12 in-stock books

thresholds by share of eligible books:
  top_10pct    score >= 0.780   -> 1 book(s)
  top_25pct    score >= 0.755   -> 2 book(s)
```

It warns when too few books are scored for the percentiles to mean anything, so
calibrate **after** the backfill, not before.

## Known gaps

- **The `Last-Modified` polling signal is only half-verified.** It returns 304
  when idle (confirmed), but it has not been observed *advancing* on a real stock
  change. The in-stock total and max product id are compared as fallbacks, and
  the hourly sweep catches anything the poll misses. Run `pooks verify-polling`
  over a period with real arrivals to settle it.
- **Blurb and renown quality are untested against a live model.** Still the
  biggest unknown: the headline no-spoiler output has never actually run.
  `pooks probe-llm` validates the credential, checks the configured models are
  still listed on OpenRouter, and prints a real blurb and renown verdict.
- **Free models are withdrawn without notice.** Both original defaults
  (`qwen-2.5-72b`, `llama-3.3-70b`) had disappeared by the time a key was
  configured. `probe-llm` flags a model OpenRouter no longer lists; check
  `GET https://openrouter.ai/api/v1/models` before changing `[llm].model`.
