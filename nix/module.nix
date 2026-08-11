{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.pooks;
  settingsFormat = pkgs.formats.toml { };
  configFile =
    if cfg.settingsFile != null then
      cfg.settingsFile
    else
      settingsFormat.generate "pooks-config.toml" cfg.settings;

  # The CLI as an operator actually needs it: same config, same database and
  # same credentials as the service.
  #
  # Without this, `pooks sweep` is not on PATH at all, and running the store
  # path directly silently uses the *wrong* database — POOKS_DATA_DIR is set on
  # the unit, not in the environment of whoever runs the command, so it falls
  # back to a path beside the read-only package. Most of the workflow (sweep,
  # backfill, calibrate, probe-llm) is manual, so this needs to just work.
  wrapped = pkgs.symlinkJoin {
    name = "pooks-cli";
    paths = [ cfg.package ];
    nativeBuildInputs = [ pkgs.makeWrapper ];
    postBuild = ''
      wrapProgram $out/bin/pooks \
        --set-default POOKS_CONFIG ${configFile} \
        --set-default POOKS_DATA_DIR /var/lib/${cfg.stateDirectory} \
        --set-default POOKS_ENV_FILE /nonexistent \
        --run 'if [ -r "${cfg.environmentFile}" ]; then set -a; . "${cfg.environmentFile}"; set +a; fi'
    '';
    inherit (cfg.package) meta;
  };

  # Shared by both units. StateDirectory gives /var/lib/pooks, which is where
  # the SQLite database and the secrets file live; the package itself sits in
  # the read-only store, hence POOKS_DATA_DIR and POOKS_CONFIG.
  common = {
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];

    environment = {
      POOKS_DATA_DIR = "/var/lib/${cfg.stateDirectory}";
      POOKS_CONFIG = configFile;
      # There is no .env in a packaged install; systemd supplies the
      # environment directly, and load_dotenv is told not to override it.
      POOKS_ENV_FILE = "/nonexistent";
    };

    serviceConfig = {
      Type = "simple";
      User = cfg.user;
      Group = cfg.group;
      StateDirectory = cfg.stateDirectory;
      WorkingDirectory = "/var/lib/${cfg.stateDirectory}";
      Restart = "on-failure";
      RestartSec = "30s";

      # Keys are kept out of the Nix store, which is world-readable. Same
      # pattern as navidrome/slskd/searx on this host.
      EnvironmentFile = cfg.environmentFile;

      NoNewPrivileges = true;
      PrivateTmp = true;
      PrivateDevices = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      RestrictNamespaces = true;
      RestrictRealtime = true;
      LockPersonality = true;
      SystemCallArchitectures = "native";
      RestrictAddressFamilies = [
        "AF_INET"
        "AF_INET6"
        "AF_UNIX"
      ];
    };
  };
in
{
  options.services.pooks = {
    enable = lib.mkEnableOption "pooks, a ranked new-arrival watcher for oldbookdepot.in";

    package = lib.mkOption {
      type = lib.types.package;
      # Built straight from this flake rather than `pkgs.pooks`, so importing
      # the module is enough. Defaulting to an overlay attribute meant a
      # consumer who imported the module without also adding
      # `nixpkgs.overlays = [ inputs.pooks.overlays.default ]` got
      # "attribute 'pooks' missing", which says nothing about the real cause.
      # The overlay still exists for anyone who wants `pkgs.pooks`.
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The pooks package to run.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "pooks";
      description = "User to run under. Created automatically when left at the default.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "pooks";
      description = "Group to run under.";
    };

    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = "pooks";
      description = "Directory under /var/lib holding the SQLite database.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.path;
      example = "/var/lib/pooks/secrets.env";
      description = ''
        File holding OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        and optionally SEARXNG_URL, HARDCOVER_API_KEY, GOOGLE_BOOKS_API_KEY.

        Deliberately not a Nix option: the store is world-readable, so keys must
        live outside it. Create it by hand with mode 0400, owned by the service
        user.
      '';
    };

    serve = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Run the read-only dashboard alongside the daemon.";
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 8080;
        description = "Port for the dashboard. Reverse-proxy it rather than exposing it.";
      };

      host = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = ''
          Bind address. Defaults to loopback on the assumption a reverse proxy
          sits in front; the dashboard has no authentication of its own.
        '';
      };
    };

    settingsFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Path to a hand-written config.toml. Takes precedence over `settings`.

        Useful because config.toml carries long explanatory comments about why
        each threshold is what it is, and generating it from Nix loses them.
      '';
    };

    settings = lib.mkOption {
      inherit (settingsFormat) type;
      default = { };
      description = ''
        config.toml contents, as Nix. Only consulted when settingsFile is null.
        See the config.toml in the repository for the full set and the reasoning
        behind the defaults.
      '';
      example = lib.literalExpression ''
        {
          notify.push_score_threshold = 0.70;
          ranking.weight_quality = 0.50;
        }
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.settingsFile != null || cfg.settings != { };
        message = "services.pooks: set either settingsFile or settings — the pipeline reads every threshold from config.toml.";
      }
    ];

    users.users = lib.mkIf (cfg.user == "pooks") {
      pooks = {
        isSystemUser = true;
        inherit (cfg) group;
        description = "pooks service user";
      };
    };

    users.groups = lib.mkIf (cfg.group == "pooks") { pooks = { }; };

    # Reads the secrets file, so it is only useful as the service user or root:
    #   sudo -u pooks pooks sweep
    environment.systemPackages = [ wrapped ];

    systemd.services.pooks = lib.recursiveUpdate common {
      description = "pooks — poll oldbookdepot.in, enrich, rank, notify";
      serviceConfig.ExecStart = "${lib.getExe cfg.package} daemon";
      # The N150 is a 4-core 6W part and the workload is I/O-bound, so this is
      # a guard against a leak rather than a real constraint.
      serviceConfig.MemoryMax = "512M";
      serviceConfig.CPUWeight = 50;
    };

    systemd.services.pooks-web = lib.mkIf cfg.serve.enable (
      lib.recursiveUpdate common {
        description = "pooks dashboard";
        # Reads the same SQLite the daemon writes; WAL handles the concurrency.
        after = common.after ++ [ "pooks.service" ];
        # Set here rather than in config.toml so the bind address still applies
        # when settingsFile points at a hand-written config.
        environment = common.environment // {
          POOKS_SERVE_HOST = cfg.serve.host;
          POOKS_SERVE_PORT = toString cfg.serve.port;
        };
        serviceConfig.ExecStart = "${lib.getExe cfg.package} serve";
        serviceConfig.MemoryMax = "256M";
      }
    );
  };
}
