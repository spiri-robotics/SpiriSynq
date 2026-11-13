# The bundled interpreters, built from pinned sources with pinned tools. The flake at the top of
# the repo exposes this as `packages.<system>.interpreters`; `make interpreters` builds it and
# copies the modules next to this file, where spirisynq-extism includes them.
{ pkgs }:
let
  inherit (pkgs) lib;
  cc = pkgs.pkgsCross.wasi32.stdenv.cc;
  llvm = pkgs."llvmPackages_${lib.versions.major cc.version}";
  lua = pkgs.fetchurl {
    url = "https://www.lua.org/ftp/lua-5.4.9.tar.gz";
    hash = "sha256-IzW2xYKlJlT5RhK/ENL0ZygF0FMpqmVosdjNnlxvuOY=";
  };
  quickjs = pkgs.fetchurl {
    url = "https://github.com/quickjs-ng/quickjs/archive/refs/tags/v0.18.0.tar.gz";
    hash = "sha256-KLzYmqNoP9KpRlN+XOPxHMRrXL8zg8dWGq7z31/58d0=";
  };
  micropython = pkgs.fetchurl {
    url = "https://github.com/micropython/micropython/releases/download/v1.29.0/micropython-1.29.0.tar.xz";
    hash = "sha256-2SWnxmTnmivfPfyyhbpeIjcEHMNaC9TuVztsVxHv7KA=";
  };
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "spirisynq-interpreters";
  version = "1";
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [ ./build.sh ./spirisynq.h ./lua/runner.c ./javascript/runner.c ./python ];
  };
  nativeBuildInputs = [ cc llvm.lld pkgs.binaryen pkgs.gnumake pkgs.python3 ];
  buildPhase = ''
    mkdir sources
    tar xzf ${lua} -C sources
    tar xzf ${quickjs} -C sources
    tar xJf ${micropython} -C sources
    LUA_SRC=$PWD/sources/lua-5.4.9 QUICKJS_SRC=$PWD/sources/quickjs-0.18.0 \
      MICROPYTHON_SRC=$PWD/sources/micropython-1.29.0 OUT=$out sh build.sh
  '';
  dontInstall = true;
}
