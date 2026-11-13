#!/bin/sh
# Builds the bundled interpreters into $OUT, from the sources in $LUA_SRC, $QUICKJS_SRC and
# $MICROPYTHON_SRC.
# default.nix
# runs it with pinned sources and tools: `make interpreters` at the top of the repo.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
CC=${CC:-wasm32-unknown-wasip1-clang}
# Reactor modules (Extism calls `_initialize`), setjmp/longjmp as WebAssembly exceptions, a
# 1 MiB stack, and nothing in the output that depends on where it was built.
CFLAGS="-O2 -mllvm -wasm-enable-sjlj -mllvm -wasm-use-legacy-eh=false \
  -ffile-prefix-map=$here=. -I$here"
LDFLAGS="-mexec-model=reactor -Wl,-z,stack-size=1048576 -lsetjmp"

finish() { # name: optimize for size and strip
  wasm-opt -Oz --strip-debug --strip-producers "$OUT/$1.raw.wasm" -o "$OUT/$1.wasm"
  rm "$OUT/$1.raw.wasm"
}

lua() {
  src=$LUA_SRC/src
  files=""
  for f in lapi lcode lctype ldebug ldo ldump lfunc lgc llex lmem lobject lopcodes lparser \
    lstate lstring ltable ltm lundump lvm lzio lauxlib lbaselib lcorolib lmathlib lstrlib \
    ltablib lutf8lib; do
    files="$files $src/$f.c"
  done
  # shellcheck disable=SC2086
  # WASI has no signals, and no process clock for randomizing sorts.
  $CC $CFLAGS -ffile-prefix-map="$LUA_SRC=lua" -I"$src" -Dl_signalT=int '-Dl_randomizePivot()=0' $files "$here/lua/runner.c" $LDFLAGS \
    -o "$OUT/lua.raw.wasm"
  finish lua
}

javascript() {
  src=$QUICKJS_SRC
  # shellcheck disable=SC2086
  $CC $CFLAGS -ffile-prefix-map="$QUICKJS_SRC=quickjs" -I"$src" -D_GNU_SOURCE \
    -D_WASI_EMULATED_PROCESS_CLOCKS -D_WASI_EMULATED_SIGNAL \
    "$src/dtoa.c" "$src/libregexp.c" "$src/libunicode.c" "$src/quickjs.c" \
    "$here/javascript/runner.c" $LDFLAGS -lwasi-emulated-process-clocks -lwasi-emulated-signal \
    -o "$OUT/javascript.raw.wasm"
  finish javascript
}

python() {
  build=$(mktemp -d)
  # MicroPython's own make rules generate its headers (qstrs, version) with python3.
  make -C "$here/python" -j"${NIX_BUILD_CORES:-4}" MPY_SRC="$MICROPYTHON_SRC" BUILD="$build" \
    OUT="$OUT" CC="$CC" \
    SYNQ_CFLAGS="$CFLAGS -ffile-prefix-map=$MICROPYTHON_SRC=micropython -ffile-prefix-map=$build=build" \
    SYNQ_LDFLAGS="$LDFLAGS"
  rm -rf "$build"
  finish python
}

mkdir -p "$OUT"
lua
javascript
python
