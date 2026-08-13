# pooks

Watches [oldbookdepot.in](https://oldbookdepot.in) for newly in-stock books,
enriches each with real rating data and used-market price comps, ranks them, and
pushes a spoiler-free digest to Telegram plus a local dashboard.

Built to run on an Intel N150 NUC. Single process, SQLite, ~150MB RSS.

> **Why it works this way:** [`docs/design.md`](docs/design.md) has the measured
> evidence behind each decision, and [`docs/adr/`](docs/adr/) records the decisions
> themselves. Contributors (human or agent) start at [`AGENTS.md`](AGENTS.md).

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
| `HARDCOVER_API_KEY` | **No genre/mood tags**, so the browse filters stay empty and the repair pass cannot fill them. Also one fewer fallback in the rating chain. |

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
| `pooks backfill --fast` | Drain the queue using only the ~1s sources (~47 min, low quality) |
| `pooks refresh` | Repair books stuck on a fallback source, a blocked lookup, or missing tags |
| `pooks blurbs --top N` | Generate blurbs for top-ranked books that lack them |
| `pooks health` | Pipeline health summary (`--push` sends it to Telegram) |
| `pooks status` | Poll state: last poll/sweep/304, and the event queue by type |
| `pooks calibrate` | Score distribution + what each threshold would actually push |
| `pooks notify --dry-run` | Render the digest without sending |
| `pooks probe-llm` | Verify the configured provider actually works |
| `pooks serve` / `pooks daemon` | Dashboard / scheduler |

## Ranking

```
score = (0.50·quality + 0.25·renown + 0.25·value)
        shrunk toward a neutral prior by confidence, × condition factor
```

All weights live in `config.toml`; `pooks rescore` applies changes for free.

`affordability` used to hold 10% and was removed: at a ₹300 knee against a
median price of ₹250 it was 1.00 for nearly every book, adding the same constant
to everything and separating nothing. Price still counts through `value`. Tested
on identical facts, dropping it left the ranking order **unchanged** while every
score fell 0.03–0.09 — so the push threshold became effectively stricter and
wants recalibrating once the catalogue is backfilled.

Two mechanisms do the heavy lifting, both there because the naive version failed
on real data:

**Bayesian shrinkage on ratings.** Rating counts span three orders of magnitude,
and thin samples are noise — Open Library reported 2.33 from 3 ratings for a work
Goodreads rates 4.11 from 7,516. Shrinking toward the global mean in proportion
to sample size stops a 5.0-from-3 outranking a 4.2-from-20,000.

**Confidence shrinkage on the composite.** Dropping missing components and
renormalising the remaining weights sounds right, but when only one component
survives the composite becomes whatever that component says — a cheap unknown
book with no rating, no renown and no comps once scored 0.95 on price alone and
went top of the ranking. The composite is now shrunk toward a neutral prior in
proportion to the evidence behind it, so unknown books sit in the middle rather
than winning.

Rating floors are per-source rather than global, for reasons that cost real
coverage before they were — see [`docs/design.md`](docs/design.md#ranking).

## Data sources

| Source | Used for | Notes |
|---|---|---|
| WooCommerce Store API | Catalogue | Public JSON. No HTML scraping. |
| Goodreads | Ratings (primary) | schema.org `aggregateRating`, work-level. `/search?q=<isbn>` redirects to the book page. |
| Hardcover | Ratings, synopsis, tags | Free key. Paste it verbatim — it already includes the `Bearer ` prefix. |
| Google Books | Synopsis, ratings | Key effectively required. |
| Open Library | Popularity | Ratings too sparse to rank on; used as a proxy only. |
| Amazon.in | **Indian price (primary)** | Organic search results only. |
| Bookswagon / bookstohome / thebookx | Indian price (fallback) | Direct ISBN search URLs. |
| SearXNG | Indian price (last resort), ISBN-less lookup | Your own instance. |
| AbeBooks | Scarcity, in-print | schema.org `ItemList`. **No prices** — see [ADR 3](docs/adr/0003-the-baseline-is-the-indian-price.md). |

Flipkart is recognised but never fetched: it answers direct requests with HTTP
529. Amazon's Product Advertising API is unusable — deprecated in May 2026 and
closed to new applicants — so the price comes from the public search page.

Two of these lie about being blocked, and one of them silently sets every price
to the same number if you trust it. [`docs/design.md`](docs/design.md) covers what
each source actually does under load.

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

## Operating

### First run on a cold catalogue

```bash
pooks backfill --fast    # ~47 min: whole catalogue ranked, deliberately low quality
pooks calibrate          # thresholds against real data
# then leave the daemon to converge on quality over the following days
```

`--fast` skips Goodreads (60s/request) and Amazon (90s), which together account
for nearly all of the ~57s/book a full pass costs; measured at 4.6s/book. What it
writes is non-primary by construction, so the repair pass upgrades it without
any extra bookkeeping.

### Push thresholds

`push_score_threshold` and `push_min_confidence` started as guesses. Run
`pooks calibrate` to see the real distribution, what the current settings would
push, and what each percentile cut-off implies:

```
in-stock books scored : 12 / 633

current settings (score >= 0.62, conf >= 0.5) would push 4 of 12 scored books

thresholds by share of eligible books:
  top_10pct    score >= 0.780   -> 1 book(s)
  top_25pct    score >= 0.755   -> 2 book(s)
```

It warns when too few books are scored for the percentiles to mean anything, so
calibrate **after** the backfill, not before.

## Known gaps

- **Free models are withdrawn without notice.** Both original defaults
  (`qwen-2.5-72b`, `llama-3.3-70b`) had disappeared by the time a key was
  configured. `probe-llm` flags a model OpenRouter no longer lists; check
  `GET https://openrouter.ai/api/v1/models` before changing `[llm].model`.
