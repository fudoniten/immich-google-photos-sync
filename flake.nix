{
  description = "Google Photos Takeout → Immich import pipeline";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      # Build the package for a given pkgs set.  Used by both the per-system
      # outputs and the overlay so the derivation is defined in one place.
      mkImmichSync = pkgs:
        let
          runtimeDeps = with pkgs; [
            python3
            rclone
            exiftool
            unzip
            immich-cli
          ];
        in
        pkgs.stdenv.mkDerivation {
          pname = "immich-google-photos-sync";
          version = "0.1.0";
          src = ./.;

          nativeBuildInputs = [ pkgs.makeWrapper ];

          dontBuild = true;

          installPhase = ''
            runHook preInstall

            install -d $out/lib/immich-sync $out/bin
            install -m 644 sync.py state.py metadata.py $out/lib/immich-sync/

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
      # Overlay: add immich-google-photos-sync to any nixpkgs set.
      #
      # In your NixOS flake:
      #   inputs.immich-sync.url = "github:fudoniten/immich-google-photos-sync";
      #
      # Then either apply the overlay:
      #   nixpkgs.overlays = [ inputs.immich-sync.overlays.default ];
      #   environment.systemPackages = [ pkgs.immich-google-photos-sync ];
      #
      # Or reference the package directly:
      #   environment.systemPackages = [
      #     inputs.immich-sync.packages.${system}.default
      #   ];
      overlays.default = final: prev: {
        immich-google-photos-sync = mkImmichSync final;
      };
    }
    //
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        immichSync = mkImmichSync pkgs;
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

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [ python3 rclone exiftool unzip immich-cli ];
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
