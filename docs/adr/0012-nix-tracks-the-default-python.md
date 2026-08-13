# 12. `nix/package.nix` tracks the default `python3` and pins no version

Status: accepted

## Context

The package was once pinned to `python312Packages`. Hydra only builds the default
Python package set at scale, so pinning to a non-default set makes every
dependency a cache miss and builds it from source. That is not merely slow: it
drags in fastapi's *test-only* closure (`inline-snapshot → isort → pylama →
vulture → pint → uncertainties → scipy`) and failed the whole `nixos-rebuild` on
a 2e-09 tolerance violation in scipy's own test suite.

## Decision

`nix/package.nix` uses `python3Packages` — nixpkgs' default — and pins no
version. Deployment parity comes from the nixpkgs revision, not from a version
number. `inputs.nixpkgs.follows` is required of consumers for the same reason:
the module resolves its package through the consumer's `pkgs`.

## Consequences

A nixpkgs bump can move the Python version underneath the package, which is
acceptable because the build runs the full test suite in `checkPhase` and every
test is offline — a broken build is real signal. The comment in `package.nix`
saying not to pin is load-bearing and should not be tidied away.
