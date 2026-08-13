# Architecture decision records

One file per decision that would be expensive to reverse or surprising to
rediscover. An ADR records *what was decided and why it is hard to undo*; the
measurements that justify it live in [`../design.md`](../design.md), so the two
do not drift into two copies of the same paragraph.

## The rule

**Any change that alters, reverses or adds an architectural decision must add a
new ADR — or supersede an existing one — in the same commit.** A decision that
only exists in a commit message is a decision the next person re-litigates.
`tests/test_docs.py` enforces the mechanical half of this (numbering, status,
sections, index) and CI runs it. It cannot enforce the judgement half, which is
why it is written down here and in [`../../AGENTS.md`](../../AGENTS.md).

Not everything is an ADR. A bug fix, a refactor that preserves behaviour, or a
tunable moved into `config.toml` is ordinary work. Ask instead: *if someone
deleted this next year because it looked redundant, would that be a regression?*
If yes, it is an ADR.

## Format

```markdown
# 12. A short sentence in the imperative or declarative

Status: accepted

## Context
What forced the decision. Prefer evidence to assertion.

## Decision
What was chosen, stated plainly.

## Consequences
What this costs, what it rules out, and what breaks if it is undone.
```

`Status:` is one of `proposed`, `accepted`, `rejected`, or
`superseded by ADR <n>`. Numbering is sequential with no gaps; the filename is
`NNNN-kebab-case-slug.md`. Never edit an accepted ADR's decision — supersede it,
so the reasoning that applied at the time is still readable.

## Index

| # | Decision | Status |
|---|---|---|
| [1](0001-enrichment-is-keyed-by-isbn.md) | Enrichment is keyed by ISBN, not product id | accepted |
| [2](0002-cost-is-decided-by-event-type.md) | Cost is decided by event type, declared as data | accepted |
| [3](0003-the-baseline-is-the-indian-price.md) | The price baseline is the Indian price, not a foreign one | accepted |
| [4](0004-a-price-must-pass-three-checks.md) | A price is accepted only if it passes three checks | accepted |
| [5](0005-failures-are-never-cached.md) | Failures are never cached | accepted |
| [6](0006-cache-lifetime-reflects-answer-quality.md) | Cache lifetime reflects how good the answer was | accepted |
| [7](0007-tags-come-from-hardcover-or-not-at-all.md) | Tags come from Hardcover, or not at all | accepted |
| [8](0008-rating-floors-are-per-source.md) | Rating floors are per-source | accepted |
| [9](0009-confidence-shrinks-the-composite.md) | Confidence shrinks the composite toward a neutral prior | accepted |
| [10](0010-blurbs-must-be-grounded.md) | A blurb is only generated when retrieved text grounds it | accepted |
| [11](0011-detection-rests-on-last-modified.md) | Detection rests on Last-Modified, corroborated by two cheap signals | accepted |
| [12](0012-nix-tracks-the-default-python.md) | `nix/package.nix` tracks the default `python3` and pins no version | accepted |
| [13](0013-arrival-dates-are-backfilled-on-the-sweep.md) | Arrival dates are backfilled on the sweep, and do not converge | accepted |
| [14](0014-taste-belongs-in-browsing.md) | Taste belongs in browsing; only measurable bias belongs in the score | accepted |
| [15](0015-observations-record-every-source-answer.md) | Every source's answer is recorded, and the shown record is derived | accepted |
| [16](0016-ratings-are-judged-per-category.md) | A rating is judged against its own category's distribution | accepted |
