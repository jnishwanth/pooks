# Integration snippet for goji (codeberg.org/jnishwanth/nix).
#
# Not meant to be imported — copy the marked pieces into the existing files.
# It follows the conventions already in goji/configuration.nix: ports declared
# in the central `ports` attrset, Caddy fronting everything on *.goji.home.arpa,
# and secrets in a hand-created file under /var/lib rather than in the store.

# ─── 1. flake.nix ────────────────────────────────────────────────────────────
#
#   inputs = {
#     nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
#     pooks = {
#       url = "github:jnishwanth/pooks";
#       inputs.nixpkgs.follows = "nixpkgs";   # one nixpkgs, not two
#     };
#   };
#
#   goji = nixpkgs.lib.nixosSystem {
#     system = "x86_64-linux";
#     specialArgs = { inherit inputs; };
#     modules = [
#       ./goji/configuration.nix
#       ./goji/hardware-configuration.nix
#       inputs.pooks.nixosModules.default          # <-- add
#     ];
#   };

# ─── 2. goji/configuration.nix — the `ports` attrset ─────────────────────────
#
#   ports = {
#     grafana = 3000;
#     silverbullet.http = 3001;
#     actualbudget.http = 3002;
#     aurral = 3003;
#     pooks = 3004;                              # <-- add (3004 is free)
#     ...
#   };

# ─── 3. goji/configuration.nix — near the other services ─────────────────────

{ config, inputs, ... }:

{
  # No overlay needed — the module builds the package itself. Add
  #   nixpkgs.overlays = [ inputs.pooks.overlays.default ];
  # only if you also want `pkgs.pooks` available elsewhere in the config.

  services.pooks = {
    enable = true;

    # Keys live outside the Nix store, which is world-readable — the same
    # reasoning as navidrome/slskd/searx on this host. Create it by hand:
    #
    #   sudo install -d -o pooks -g pooks -m 0750 /var/lib/pooks
    #   sudo install -m 0400 -o pooks -g pooks /dev/null /var/lib/pooks/secrets.env
    #   sudoedit /var/lib/pooks/secrets.env
    #
    #     OPENROUTER_API_KEY=sk-or-v1-...
    #     TELEGRAM_BOT_TOKEN=...
    #     TELEGRAM_CHAT_ID=8008294140
    #     SEARXNG_URL=http://search.goji.home.arpa
    #     HARDCOVER_API_KEY=...
    #     GOOGLE_BOOKS_API_KEY=...
    environmentFile = "/var/lib/pooks/secrets.env";

    # config.toml carries long comments explaining why each threshold is what it
    # is — several encode bugs that were expensive to find. Pointing at the file
    # keeps them; `settings` as Nix would discard them.
    settingsFile = "${inputs.pooks}/config.toml";

    serve = {
      enable = true;
      port = config.goji.ports.pooks or 3004;
      host = "127.0.0.1"; # Caddy fronts it; the dashboard has no auth of its own
    };
  };

  # ─── 4. Caddy virtualHosts ────────────────────────────────────────────────
  #
  #   "http://pooks.goji.home.arpa" = mkProxy ports.pooks;

  # ─── 5. First run, once deployed ──────────────────────────────────────────
  #
  #   sudo -u pooks pooks probe-llm     # confirm the key and models work
  #   sudo -u pooks pooks sweep         # seed ~634 in-stock books (suppressed
  #                                     # as backfill: no inference, no pushes)
  #   sudo -u pooks pooks enrich --limit 50   # warm the cache in batches
  #   sudo -u pooks pooks calibrate     # tune thresholds once enough is scored
  #
  # The full backfill is ~630 books and paces at 90s per Amazon lookup, so it
  # runs for many hours. It is safe to do in chunks; everything is cached by
  # ISBN, so re-running only picks up what is missing.
}
