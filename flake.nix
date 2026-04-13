{
  description = "Google Photos Takeout → Immich import pipeline";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # All tools that sync.py shells out to.
        # immich-cli is the @immich/cli npm package, packaged in nixpkgs as immich-cli.
        runtimeDeps = with pkgs; [
          python3
          rclone
          exiftool
          unzip
          immich-cli
        ];

        immichSync = pkgs.stdenv.mkDerivation {
          pname = "immich-google-photos-sync";
          version = "0.1.0";
          src = ./.;

          nativeBuildInputs = [ pkgs.makeWrapper ];

          dontBuild = true;

          installPhase = ''
            runHook preInstall

            install -d $out/lib/immich-sync $out/bin
            install -m 644 sync.py state.py metadata.py $out/lib/immich-sync/

            # Wrap python3 so that:
            #   - all runtime tools are on PATH
            #   - our modules (state, metadata) are importable
            makeWrapper ${pkgs.python3}/bin/python3 $out/bin/immich-sync \
              --add-flags "$out/lib/immich-sync/sync.py" \
              --prefix PATH : "${pkgs.lib.makeBinPath runtimeDeps}" \
              --set PYTHONPATH "$out/lib/immich-sync"

            runHook postInstall
          '';

          meta = with pkgs.lib; {
            description = "Import Google Photos Takeout archives into a self-hosted Immich instance";
            license = licenses.mit;
            mainProgram = "immich-sync";
          };
        };
      in
      {
        packages = {
          default = immichSync;
          immich-sync = immichSync;
        };

        apps.default = {
          type = "app";
          program = "${immichSync}/bin/immich-sync";
        };

        # Development shell: all runtime tools available, run scripts directly
        # from the source tree without installing.
        #
        # Usage:
        #   nix develop
        #   python3 sync.py --help
        #   python3 sync.py --dry-run "gdrive:Takeout"
        devShells.default = pkgs.mkShell {
          packages = runtimeDeps;
          shellHook = ''
            echo "immich-google-photos-sync dev shell"
            echo ""
            echo "First-time setup:"
            echo "  rclone config          # add your Google Drive remote"
            echo "  immich login <url> <api-key>"
            echo ""
            echo "Run the sync:"
            echo "  python3 sync.py --help"
            echo "  python3 sync.py --dry-run \"gdrive:Takeout\""
          '';
        };
      });
}
