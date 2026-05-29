{
  description = "Monitoring & Finance stack";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = {
    self,
    nixpkgs,
    flake-utils,
  }:
  # ── System modules (devShell, package) ──────────────────
    flake-utils.lib.eachSystem ["aarch64-linux" "x86_64-linux"] (
      system: let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;
      in {
        # ── Finance-etl package ─────────────────────────────────────────────
        packages.finance-etl = pkgs.callPackage ./nix/package.nix {
          dataDir = "/var/lib/finance";
        };

        packages.default = self.packages.${system}.finance-etl;

        # ── DevShell ────────────────────────────────────────────────────────
        devShells.default = pkgs.mkShell {
          name = "monitoring";
          packages = [
            (python.withPackages (ps:
              with ps; [
                psycopg
                httpx
                pyyaml
                pytest
                python-dateutil
              ]))
            pkgs.ruff
            pkgs.postgresql_16
            pkgs.gnumake
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/finance:$PYTHONPATH"
            echo "monitoring devShell ready — python $(python3 --version)"
          '';
        };
      }
    )
    # ── System independent outputs (nixosModules) ──────────────────────
    // {
      # ── NixOS Module ───────────────────────────────────────────────────────
      nixosModules.finance = {
        config,
        lib,
        pkgs,
        ...
      } @ args:
        import ./nix/module.nix (args // {inputs.finance-stack = self;});

      nixosModules.default = self.nixosModules.finance;
    };
}
