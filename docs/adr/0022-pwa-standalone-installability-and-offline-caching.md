# 22. Dashboard is installable as a PWA with network-first offline caching

Status: accepted

## Context

The dashboard is the primary interface for browsing ranked book arrivals, filtering
by tags, and reviewing catalogue scores. When accessed on mobile phone browsers,
running inside standard browser chrome (URL bar, navigation buttons, sheet toolbars)
wastes vertical screen space and restricts the mobile experience to a browser bookmark.

Secondhand books at oldbookdepot.in are physical items with single-copy inventory that
can sell out quickly. A naive cache-first service worker would display stale stock
and misleading availability to users when the shop catalogue changes. Conversely,
having no service worker or manifest prevents Android Chrome from offering standalone
WebAPK installation and app-drawer integration, and provides no fallback when network
connectivity drops.

## Decision

The dashboard is made installable as a Progressive Web App (PWA) with a network-first
service worker and self-hosted assets:

1. **Web App Manifest and iOS Meta**: `/manifest.webmanifest` specifies `display: standalone`,
   theme and background colors matching the warm palette (`#fbfaf8`), and multi-size icon
   definitions (192x192 and 512x512 with `purpose: "any maskable"`). Companion Apple
   touch icon and mobile-web-app tags are declared in `index.html`.
2. **Network-First Service Worker**: `/sw.js` attempts a live network fetch for all requests
   first. On HTTP 200 responses, the cache (`pooks-v1`) is updated. Only when the network is
   unreachable does the service worker fall back to the cached document.
3. **Subtle Offline Notice**: An offline indicator badge (`#offline-badge`) is rendered when
   the browser loses connectivity, notifying the user that they are viewing cached arrivals
   rather than live inventory.
4. **Hermetic & Zero-Dependency**: All PWA icons and scripts are self-hosted with no external
   CDN dependencies, preserved by `tests/test_serve.py`, and packaged for deployment via
   `nix/package.nix`.

## Consequences

Mobile users can install pooks directly to their phone home screens and app drawers as a
clean, full-screen standalone application.

Inventory freshness is preserved while online, while offline browsing degrades gracefully
to the most recently viewed snapshot with explicit indication. Android Chrome requires
access over a secure HTTPS origin (such as Tailscale HTTPS or a reverse proxy) to mint
the native WebAPK.
