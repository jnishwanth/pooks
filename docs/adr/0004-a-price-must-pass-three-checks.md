# 4. A price is accepted only if it passes three checks

Status: accepted

## Context

The rebuild reproduced the original bug before fixing it. Scanning retailer pages
for currency patterns priced *every* book at ₹500 — a promotional banner, "FLAT
10% OFF (Up to ₹500)", sitting above the product on every page. Taking the
smallest figure instead picks shipping (₹40 delivery beat a ₹349 book). Worse,
Bookswagon returned the right book as an out-of-stock "International" edition at
₹12,211, which would have made the shop look 98% cheaper.

## Decision

A price is accepted only when all three hold:

1. **product-scoped markup** — JSON-LD `Offer` or a known per-retailer selector,
   never loose page text;
2. **in stock** — an unavailable listing is not an alternative the buyer has;
3. **title matches** — via the same fuzzy ladder used for ratings.

Failing any of them yields no price. There is deliberately no text-scanning
fallback.

## Consequences

Coverage is lower than a permissive scan would report, and that is the trade:
a missing price is visible and harmless, a fabricated one silently corrupts every
ranking it touches. Adding a new retailer means adding a selector or relying on
its JSON-LD — not widening the parser.
