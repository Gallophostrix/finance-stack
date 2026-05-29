{
  pkgs,
  lib,
  dataDir,
}: let
  python = pkgs.python312;
in
  python.pkgs.buildPythonApplication {
    pname = "finance-etl";
    version = "0.1.0";

    # ── Source ────────────────────────────────────────────────────────────────
    src = lib.cleanSourceWith {
      src = ../.;
      filter = path: type: let
        relPath = lib.removePrefix (toString ../.) (toString path);
      in
        relPath
        == "/finance" # parent directory
        || relPath == "/finance/pyproject.toml"
        || lib.hasPrefix "/finance/etl" relPath # Python ETL package
        || lib.hasPrefix "/finance/scripts" relPath # CLI scripts
        || lib.hasPrefix "/finance/sql" relPath; # SQL init scripts
    };

    pyproject = true;

    # pyproject.toml is in finance/ not at the root of the src
    sourceRoot = "source/finance";

    # ── Dependencies ───────────────────────────────────────────────────────────

    build-system = with python.pkgs; [
      setuptools
    ];

    # propagatedBuildInputs = dependencies available at runtime
    # (propagated to packages that depend on finance-etl)
    propagatedBuildInputs = with python.pkgs; [
      psycopg # PostgreSQL driver v3
      httpx # HTTP client (blockchain fetchers)
      pyyaml # YAML assets parsing
      python-dateutil # TWR/MWR relative delta
    ];

    # ── Build options ──────────────────────────────────────────────────────

    # strictDeps separating build-time and runtime deps
    # avoids phantom dependencies
    strictDeps = true;

    # Check that `import etl` works after installation
    # Detects broken imports at build time rather than runtime
    pythonImportsCheck = ["etl"];

    # ── dataDir injection ──────────────────────────────────────────────────
    #
    # Passed as an argument from module.nix
    # Injected into the wrappers of the commands that need it
    # via postInstall — after buildPythonApplication has created the
    # entry points
    #
    # Using --assets (sync_dims and fetch_cardano)
    # to refer to the YAML asset definitions
    nativeBuildInputs = [pkgs.makeWrapper];

    postInstall = ''
      # finance-sync-dims : ajoute --assets <dataDir>/assets par défaut
      wrapProgram $out/bin/finance-sync-dims \
        --add-flags "--assets ${dataDir}/assets"

      # finance-fetch-cardano : idem
      wrapProgram $out/bin/finance-fetch-cardano \
        --add-flags "--assets ${dataDir}/assets"
    '';

    meta = {
      description = "Finance ETL — fetch, derive, compute returns";
    };
  }
