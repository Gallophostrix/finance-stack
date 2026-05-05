{
  description = "Monitoring & Finance stack — devShell";

  inputs = {
    nixpkgs.url     = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "aarch64-linux" "x86_64-linux" ] (system:
      let
        pkgs   = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;
      in {
        devShells.default = pkgs.mkShell {
          name = "monitoring";
          packages = [
            # Python + deps
            (python.withPackages (ps: with ps; [
              psycopg2
              httpx
              pyyaml
              pytest
              pip
            ]))
            # Tools
            pkgs.ruff
            pkgs.postgresql_16  # psql CLI
            pkgs.docker-compose
            pkgs.gnumake
          ];

          shellHook = ''
            export PYTHONPATH="$PWD/finance:$PYTHONPATH"
            echo "monitoring devShell ready — python $(python3 --version)"
          '';
        };
      }
    );
}
