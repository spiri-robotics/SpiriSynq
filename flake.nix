{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";

      tex = nixpkgs.legacyPackages.${system}.texlive.combine {
        inherit (nixpkgs.legacyPackages.${system}.texlive)
          scheme-basic latexmk
          # fonts
          cmap cm-super lm collection-fontsrecommended
          # sphinx latex template deps
          capt-of fancyhdr fancybox fancyvrb fncychap float framed geometry
          hyperref listings multirow needspace oberdiek parskip pict2e ellipse
          tabulary titlesec upquote varwidth wrapfig xcolor
          amsmath eqparbox threeparttable mdwtools etoolbox
          collection-latexrecommended;
      };
    in
    {
      # The interpreters spirisynq-extism bundles: `make interpreters`.
      packages.${system}.interpreters = import ./rust/spirisynq-extism/interpreters {
        pkgs = nixpkgs.legacyPackages.${system};
      };

      devShells.${system} = {
        # The Rust core: toolchain, coverage and LLVM tools matching rustc's LLVM. `make
        # rust-test` runs in it: `nix develop .#rust -c make rust-test`.
        rust = nixpkgs.legacyPackages.${system}.mkShell {
          buildInputs = with nixpkgs.legacyPackages.${system}; [
            cargo rustc clippy rustfmt cargo-llvm-cov llvmPackages_23.llvm gnumake uv
          ];
        };

        default = nixpkgs.legacyPackages.${system}.mkShell {
          buildInputs = [
            nixpkgs.legacyPackages.${system}.python3
            nixpkgs.legacyPackages.${system}.python3Packages.sphinx
            nixpkgs.legacyPackages.${system}.python3Packages.sphinx-rtd-theme
            tex
          ];
        };
      };
    };
  }
