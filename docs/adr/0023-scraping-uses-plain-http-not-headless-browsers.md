# 23. Scraping uses plain HTTP, not headless browsers

Status: accepted

## Context

Evaluated 2026-09-08 against live Amazon.in, Goodreads and Flipkart endpoints
to determine whether lightweight headless browsers (Lightpanda), stealth engines
or browser automation (Playwright Chromium) could bypass bot gating or unlock
unfetchable retailers.

Empirical measurements showed:
1. **Amazon.in** serves full server-side rendered HTML (542KB, 9 organic results)
   to plain HTTP in 1.17s. Its sustained-volume 503 is an IP-level rate limit, not
   a JavaScript challenge. Lightpanda took 1.50s (2.55s in bursts) and offered no
   bypass.
2. **Goodreads** AWS WAF actively fingerprinted Playwright Chromium and served an
   HTTP 202 soft block. Lightpanda received an AWS WAF challenge script
   (`AwsWafIntegration`) and failed to parse the page (2.4KB stub). `PoliteClient`
   via `httpx` retrieved the full 853KB page with schema JSON-LD in 4.28s.
3. **Flipkart** gates product queries behind Google reCAPTCHA Enterprise
   (`<h1>Are you a human?</h1>`), blocking all automated browser and HTTP clients
   identically.
4. **Operational overhead**: Browsers add 80-250MB binary footprints, higher RAM
   usage, and break the minimal Hydra/Nix package build (`nix/package.nix`), all
   for a pipeline handling ~15 books a day.

## Decision

Price and rating scraping continues using plain HTTP (`PoliteClient` on `httpx`)
with full browser headers, paced per-host intervals, and soft-block circuit
breakers. Headless browsers and browser automation are rejected.

If throughput ever needs raising during bulk backfills, the lever is IP
distribution (rotating proxies or SearXNG instances), not client-side DOM
rendering.

## Consequences

- No external browser binaries, Node bridges, or C-extension toolchains in the
  runtime closure.
- Nix packaging remains pure Python on standard `python3Packages`.
- Flipkart remains unpolled (`UNFETCHABLE_DOMAINS`).
- Amazon pacing remains at 90s with jitter; Goodreads at 60s.

See [`../design.md`](../design.md#headless-and-stealth-browsers-do-not-bypass-the-rate-limit) for the measurements.
