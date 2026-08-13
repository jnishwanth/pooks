# Working on pooks

For anyone — human or agent — changing this repository. It records the
conventions that produced the current code, most of which exist because
something went wrong first.

## What this is

A single-process pipeline that watches one shop's WooCommerce Store API for
newly in-stock books, enriches each with rating and price data, ranks them, and
pushes a digest. `README.md` is the user guide, [`docs/design.md`](docs/design.md)
holds the measured evidence behind the design, and [`docs/adr/`](docs/adr/)
records the decisions.

## The standing objective

**Keep the repo as minimal as needed. Avoid feature creep.** Prefer a change
that *deletes* a concept while adding a capability over one that adds both. The
rule that has paid off repeatedly is *one definition per rule read from two
places* — not "no duplication anywhere". Hoisting a value with exactly one reader
into a shared module adds indirection and deletes nothing.

## The hard rule: decisions get ADRs

**A change that alters, reverses or adds an architectural decision must add a new
ADR — or supersede an existing one — in the same commit.** Not every change
qualifies; the test is in [`docs/adr/README.md`](docs/adr/README.md). A decision
recorded only in a commit message is one the next person re-litigates.
`tests/test_docs.py` enforces numbering, status, sections and the index. It
cannot enforce the judgement, which is why this paragraph exists.

If you change behaviour that `docs/design.md` describes, update that too. Its
claims are measurements, so a stale one is worse than no note at all.

## Verifying a change

Two things are expected of any non-trivial change, and neither is optional
because the unit tests alone have repeatedly proved insufficient here.

**Mutation-check every new assertion.** Break the thing the test exists to
protect and confirm *that* test fails. A test that passes against a deliberately
broken implementation is documentation, not a check. Traps found the hard way:

- A single-character mutation leaves the file size unchanged, and CPython
  validates a `.pyc` by `(mtime, size)` at one-second granularity — so restoring
  within the same second **reuses the mutated bytecode** and the test keeps
  failing against correct source. `touch` the file or clear `__pycache__` after
  any revert.
- Never `git checkout <file>` to undo a mutation: it reverts *all* uncommitted
  work in that file. Copy to a scratch path and copy back.
- A fixture split evenly across a comparison cannot distinguish its direction.
  Two rows on the expected side and one on the other is the minimum.
- A boundary mutation (`>=` → `>`) survives unless an input lands exactly *on*
  the boundary.

**Differential against the live database.** `data/pooks.db` is a real catalogue.
Copy it first — `connect()` writes on open — then run the change both ways and
diff:

```bash
sqlite3 data/pooks.db ".backup '/tmp/check/pooks.db'"
POOKS_DATA_DIR=/tmp/check uv run pooks rescore    # then diff the scores table
POOKS_DATA_DIR=/tmp/check uv run pooks health     # renders a fixed block; diff stdout
```

Use `.backup`, not `cp`. The schema sets `journal_mode = WAL`, so recent writes
live in `pooks.db-wal` until a checkpoint — a plain `cp` of the one file silently
gives you a stale snapshot, and it looks like the feature you just added wrote
nothing. This is only visible while something else is writing, which is exactly
when you are most likely to be checking.

For a scoring or persistence change, a full `rescore` followed by a row-for-row
score comparison is the strongest oracle available and takes about twenty lines.
Note that a rescore legitimately rewrites `breakdown_json` for rows written
before a component was retired, so compare the *parsed* breakdown or a fixed
column set — and to isolate your own change, run the same differential in a
`git worktree` at the previous commit and compare the two results.

A differential proves no regression; only the mutations prove the tests pin
anything. Both, not either.

## Test quality

Never assert on implementation source text — greps, snapshots of code, or
"function X is mentioned". Matching text can be dead, and a behaviour-preserving
refactor changes it. Execute an interface and assert on observable behaviour.
Reading a file is legitimate when the file *is* the deliverable, as in
`tests/test_docs.py`, which owns the ADR format as a contract.

For a regression, reproduce the reported failure first: the test should fail
before the fix and pass after it.

## Standing detectors

Cheap to run at the start of a change, and each has found real dead code:

1. **Write-only dataclass fields** — an AST pass collecting every `ast.Attribute`
   in Load context across `src`, `tests` and templates, reporting declared fields
   whose name never appears. Group the names **by owning class** and hand-check
   the ones owned by more than one: a plain scan hid five dead fields for
   fourteen iterations because `url` was a live field on four other classes.
2. **Keyword parameters no call site passes** — an optional parameter can hide a
   second, divergent implementation of a user-visible behaviour.
3. **Divergent construction sites** — collect every call constructing a given
   dataclass and diff the keyword sets; a site passing a strict subset is
   suspect. This is what caught `pooks notify` silently defaulting a field that
   `process` populated, so the two rendered different cards.

They came back empty as of the last sweep. Run them once, then look for
duplication *inside* function bodies, which is where the remaining wins are.

## Traps

- **Write-only state comes in chains.** Deleting a field without following its
  upstream leaves functions computing values for nobody. Trace the removed
  reader's own producer.
- **A dataclass serialised into a JSON column is a schema that drifts.**
  `scores.breakdown_json` still carries `affordability`, retired long ago, so any
  `Cls(**json.loads(column))` needs an explicit unknown-key policy or it crashes
  on old rows. `ScoreBreakdown.from_stored` is the pattern.
- **A boolean property cannot narrow the fields it tested.** `has_rating`,
  `has_price` and `configured` all answer a question about optional attributes
  without making them non-optional for the caller. Where the caller needs the
  values, return them: `BookFacts.rating_with_count`,
  `TelegramNotifier._credentials`.
- **`data/pooks.db` is legacy-schema** — it still has columns since removed from
  `schema.sql`. Test schema changes against both a fresh database and that shape.
- **`serve.app._open` calls `connect()` inside every HTTP request**, so anything
  added to `_migrate` runs per request. Probe before writing.
- **TOML subsection footgun**: every plain key of `[ratings]` must sit *above*
  `[ratings.min_count_by_source]`, or it is parsed into the subsection. This
  silently emptied the rating chain once; `tests/test_config.py` guards it.
- **Tests read the real `config.toml` at import time** (`test_quality.py`,
  `test_enrich.py`, `test_cache_roundtrip.py`). Changing a shipped default can
  move a test.
- **Ad-hoc scripts must run from the repo root** via `uv run python`; `cd /tmp`
  first breaks the `pooks` import even under `uv run`.
- The live pipeline is **cold** — a small fraction of the catalogue is enriched.
  Anything reasoning about enriched data has a tiny real sample, so a live
  differential can prove absence of regression but rarely confirms a fix.

## Commands

```bash
uv sync
uv run pytest -q                 # fast and offline; keep it that way
uv run ruff check . && uv run ruff format --check .
uv run mypy                      # strict, src only; must stay at zero
uv run pytest -q --cov=pooks     # floor in [tool.coverage.report]
```

Every test is offline. Adding one that needs the network breaks the Nix build,
which runs the suite in `checkPhase` and is real deployment signal. Raise the
coverage floor when a change adds real coverage; never lower it to go green.
