{
  description = "pooks — ranked new-arrival pipeline for oldbookdepot.in";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };
    in
    {
      overlays.default = final: prev: {
        pooks = final.callPackage ./nix/package.nix { };
      };

      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.callPackage ./nix/package.nix { };
          pooks = pkgs.callPackage ./nix/package.nix { };
        }
      );

      nixosModules.default = import ./nix/module.nix;
      nixosModules.pooks = import ./nix/module.nix;

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.uv
              pkgs.python312
              pkgs.ruff
            ];
          };
        }
      );

      formatter = forAllSystems (system: (pkgsFor system).nixfmt-tree);
    };
}
