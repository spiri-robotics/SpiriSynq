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
          capt-of fancyhdr fancybox fncychap float framed geometry
          hyperref multirow needspace oberdiek parskip
          tabulary titlesec upquote varwidth wrapfig
          amsmath eqparbox threeparttable mdwtools etoolbox;
      };
    in
    {
      devShells.${system}.default = nixpkgs.legacyPackages.${system}.mkShell {
        buildInputs = [
          nixpkgs.legacyPackages.${system}.python3
          nixpkgs.legacyPackages.${system}.python3Packages.sphinx
          nixpkgs.legacyPackages.${system}.python3Packages.sphinx-rtd-theme
          tex
        ];
      };
    };
  }
