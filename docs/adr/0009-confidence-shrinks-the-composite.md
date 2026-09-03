# 9. Confidence shrinks the composite toward a neutral prior

Status: accepted

## Context

Dropping missing components and renormalising the remaining weights sounds right,
but when only one component survives the composite becomes whatever that
component says. A cheap unknown book with no rating, no renown and no comps
scored 0.95 on price alone and went top of the ranking.

## Decision

Confidence is computed separately from the score, and the renormalised composite
is shrunk toward `NEUTRAL_PRIOR` in proportion to it — the same logic as the
Bayesian rating shrinkage, applied one level up. Confidence also gates pushes
independently, through `rank.score.pushable`.

## Consequences

Unknown books sit in the middle of the ranking rather than winning or losing on
no information, and a thin-evidence book cannot dominate a digest. Two knobs
(`NEUTRAL_PRIOR`, `EVIDENCE_SATURATION`) are constants rather than config,
because tuning them changes what a score *means* rather than how it is weighted —
weights live in `config.toml` and `pooks rescore` applies them for free.

Keeping confidence out of the score is deliberate: blended in, it would be
invisible, and `pooks calibrate` could not report the two gates separately.
