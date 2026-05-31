{
  config,
  lib,
  pkgs,
  inputs,
  ...
}: let
  cfg = config.services.finance;

  # ── ETL package ───────────────────────────────────────────────────────────
  financeEtl =
    pkgs.callPackage
    (inputs.finance-stack + "/nix/package.nix")
    {inherit (cfg) dataDir;};

  # ── mkFinanceService helper ───────────────────────────────────────────────

  # Factorising the one-shot systemd services declaration
  # Using peer auth through socket Unix (no password in PG_DSN)
  # PostgreSQL socket in /run/postgresql
  mkFinanceService = {
    name,
    description,
    script,
    needsNetwork ? false,
  }: {
    inherit description;
    after =
      ["postgresql.service"]
      ++ lib.optional needsNetwork "network-online.target";
    wants =
      lib.optional needsNetwork "network-online.target";
    requires = ["postgresql.service"];

    # ── Common Environment for all systemd services ───────────────────
    serviceConfig = {
      User = cfg.user;
      Group = cfg.user;
      Type = "oneshot";
      RemainAfterExit = false;

      Environment =
        lib.optional (cfg.coinGeckoKeyFile != "")
        "COINGECKO_KEY_FILE=${cfg.coinGeckoKeyFile}";

      WorkingDirectory = cfg.dataDir;

      # Systemd isolation
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ReadWritePaths = [cfg.dataDir];

      ExecStart = pkgs.writeShellScript "finance-${name}" ''
        set -euo pipefail
        export PATH="${pkgs.postgresql_16}/bin:$PATH"
        export PG_DSN="postgresql:///${cfg.postgresDb}?host=/run/postgresql&user=${cfg.postgresUser}"
        ${script}
      '';
    };
  };
in {
  # ── Options ───────────────────────────────────────────────────────────────

  options.services.finance = {
    enable = lib.mkEnableOption "Finance ETL stack";

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/finance";
      description = "Mutable data directory (CSV, YAML assets).";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "finance";
      description = "Finance system user.";
    };

    postgresUser = lib.mkOption {
      type = lib.types.str;
      default = "finance";
      description = "PostgreSQL user.";
    };

    postgresDb = lib.mkOption {
      type = lib.types.str;
      default = "finance";
      description = "PostgreSQL database name.";
    };

    grafanaSecretsFile = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = ''
        Path to the sops-nix file containing FINANCE_DB_PASSWORD.
        Required only if Grafana accesses PostgreSQL via TCP.
        Leave empty if Grafana is on the same host (peer auth sufficient).
      '';
    };

    coinGeckoKeyFile = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Path to the sops-nix file containing the CoinGecko API key (raw).";
    };

    timerOnCalendar = lib.mkOption {
      type = lib.types.str;
      default = "*-*-01 00:01:00";
      description = ''
        Systemd timer calendar for the monthly timer.
        Default: the 1st of each month at 00:01.
        See systemd.time(7) for syntax.
      '';
    };
  };

  # ── Implementation ────────────────────────────────────────────────────────

  config = lib.mkIf cfg.enable {
    # ── User and system group ────────────────────────────────────────────────
    users.users.${cfg.user} = {
      # isSystemUser => no home, no login
      isSystemUser = true;
      group = cfg.user;
      # Access to PostgreSQL socket
      extraGroups = ["postgres"];
      description = "Finance ETL service user";
    };
    users.groups.${cfg.user} = {};

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    services.postgresql = {
      enable = true;
      package = pkgs.postgresql_16;

      # Listen only on Unix socket — no TCP
      # Grafana on the same host can also use the socket.
      # If Grafana is on a different host, set enableTCPIP = true
      # and configure grafanaSecretsFile
      enableTCPIP = false;

      ensureUsers = [
        {
          name = cfg.postgresUser;
          ensureDBOwnership = true;
        }
        {
          name = "grafana";
          ensureDBOwnership = false;
        }
      ];
      ensureDatabases = [cfg.postgresDb];

      # Peer auth: the system user `finance` can connect to the `finance` DB
      # without a password via the Unix socket.
      authentication = lib.mkAfter ''
        local ${cfg.postgresDb} ${cfg.postgresUser} peer
        host  ${cfg.postgresDb} grafana localhost trust
      '';
    };

    # ── DB migrations ─────────────────────────────────────────────────────────

    # Apply SQL migrations on startup.
    # Creates a schema_migrations table to track applied migrations.

    systemd.services.finance-migrate =
      lib.recursiveUpdate
      (mkFinanceService {
        name = "migrate";
        description = "Finance : PostgreSQL migrations";
        script = ''
          psql -d ${cfg.postgresDb} -c "
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
              version    INTEGER PRIMARY KEY,
              name       TEXT NOT NULL,
              applied_at TIMESTAMPTZ DEFAULT NOW()
            );
          "

          for migration in ${financeEtl}/lib/finance-etl/sql/*.sql; do
            version=$(basename $migration | grep -oP '^\d+')
            name=$(basename $migration .sql)

            already_applied=$(psql -d ${cfg.postgresDb} -tAc \
              "SELECT COUNT(*) FROM public.schema_migrations WHERE version = $version")

            if [ "$already_applied" = "0" ]; then
              echo "Applying migration $name..."
              psql -d ${cfg.postgresDb} -f $migration
              psql -d ${cfg.postgresDb} -c \
                "INSERT INTO public.schema_migrations (version, name) VALUES ($version, '$name')"
              echo "Migration $name applied."
            else
              echo "Migration $name already applied, skipping."
            fi
          done

          # Grants Grafana — idempotents
          psql -d ${cfg.postgresDb} -c "
            GRANT CONNECT ON DATABASE ${cfg.postgresDb} TO grafana;
            GRANT USAGE ON SCHEMA core, market, derived TO grafana;
            GRANT SELECT ON ALL TABLES IN SCHEMA core TO grafana;
            GRANT SELECT ON ALL TABLES IN SCHEMA market TO grafana;
            GRANT SELECT ON ALL TABLES IN SCHEMA derived TO grafana;
          "
        '';
      })
      {
        wantedBy = ["multi-user.target"];
        serviceConfig = {
          StateDirectory = "finance";
          StateDirectoryMode = "0750";
        };
      };

    # ── One-shot ETL services ─────────────────────────────────────────────────

    # Each ETL command is a systemd one-shot service
    # Manually launchable via:
    #   systemctl start finance-fetch-cardano
    #   systemctl start finance-monthly

    # Not wantedBy (does not start automatically)

    systemd.services.finance-import-balances = mkFinanceService {
      name = "import-balances";
      description = "Finance : Import balances";
      needsNetwork = true;
      script = "${financeEtl}/bin/finance-import-balances";
    };

    systemd.services.finance-import-flows = mkFinanceService {
      name = "import-flows";
      description = "Finance : Import flows";
      needsNetwork = true;
      script = "${financeEtl}/bin/finance-import-flows";
    };

    systemd.services.finance-fetch-prices = mkFinanceService {
      name = "fetch-prices";
      description = "Finance : CoinGecko EOD price fetch";
      needsNetwork = true;
      script = "${financeEtl}/bin/finance-fetch-prices";
    };

    systemd.services.finance-fetch-bitcoin = mkFinanceService {
      name = "fetch-bitcoin";
      description = "Finance : Bitcoin (Mempool) fetch";
      needsNetwork = true;
      script = "${financeEtl}/bin/finance-fetch-bitcoin";
    };

    systemd.services.finance-fetch-cardano = mkFinanceService {
      name = "fetch-cardano";
      description = "Finance : Cardano (Koios) fetch";
      needsNetwork = true;
      script = "${financeEtl}/bin/finance-fetch-cardano";
    };

    systemd.services.finance-fetch-avalanche = mkFinanceService {
      name = "fetch-avalanche";
      description = "Finance : Avalanche (Routescan) fetch";
      needsNetwork = true;
      script = "${financeEtl}/bin/finance-fetch-avalanche";
    };

    systemd.services.finance-fetch-xrpl = mkFinanceService {
      name = "fetch-xrpl";
      description = "Finance : XRP (XRPL RPC) fetch";
      needsNetwork = true;
      script = "${financeEtl}/bin/finance-fetch-xrpl";
    };

    systemd.services.finance-derive = mkFinanceService {
      name = "derive";
      description = "Finance : native → EUR projection";
      script = "${financeEtl}/bin/finance-derive";
    };

    systemd.services.finance-returns = mkFinanceService {
      name = "returns";
      description = "Finance : TWR/MWR calculation";
      script = "${financeEtl}/bin/finance-returns";
    };

    # ── Monthly workflow ───────────────────────────────────────────────────────
    #
    # Executes fetch-prices → fetch-crypto → derive → returns
    systemd.services.finance-monthly =
      mkFinanceService {
        name = "monthly";
        description = "Finance : monthly workflow";
        needsNetwork = true;
        script = ''
          date=$(date +%Y-%m-01)
          balances_file="${cfg.dataDir}/data/balances/$date.csv"
          flows_file="${cfg.dataDir}/data/flows/$date.csv"

          if [ -f "$balances_file" ]; then
            echo "==> import-balances"
            ${financeEtl}/bin/finance-import-balances --date $date --file $balances_file
          else
            echo "No balances file for $date, skipping"
          fi

          if [ -f "$flows_file" ]; then
            echo "==> import-flows"
            ${financeEtl}/bin/finance-import-flows --date $date --file $flows_file
          else
            echo "No flows file for $date, skipping"
          fi

          echo "==> fetch-prices"
          ${financeEtl}/bin/finance-fetch-prices

          echo "==> fetch-bitcoin"
          ${financeEtl}/bin/finance-fetch-bitcoin

          echo "==> fetch-cardano"
          ${financeEtl}/bin/finance-fetch-cardano

          echo "==> fetch-avalanche"
          ${financeEtl}/bin/finance-fetch-avalanche

          echo "==> fetch-xrpl"
          ${financeEtl}/bin/finance-fetch-xrpl

          echo "==> derive"
          ${financeEtl}/bin/finance-derive

          echo "==> returns"
          ${financeEtl}/bin/finance-returns

          echo "==> done"
        '';
      }
      // {
        after = [
          "network-online.target"
          "postgresql.service"
          "finance-migrate.service"
        ];
      };

    # ── Monthly timer ─────────────────────────────────────────────────────────
    systemd.timers.finance-monthly = {
      description = "Finance : monthly timer";
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = cfg.timerOnCalendar;
        # Persistent = true : if the system was off at the scheduled time,
        # the timer will be triggered on the next boot.
        Persistent = true;
        Unit = "finance-monthly.service";
      };
    };

    # ── System-wide available package ──────────────────────────────────────
    #
    # Make finance-* available system-wide,
    # allowing to run them from any shell.
    environment.systemPackages = [financeEtl];
  };
}
