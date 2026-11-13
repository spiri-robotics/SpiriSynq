// MicroPython for SpiriSynq: the language, and only the modules the docs promise (array,
// binascii, cmath, collections, hashlib, heapq, json, math, random, re, struct, and sys, plus
// io, which json needs). No
// files, no OS, no time, no imports from anywhere else.

#include <stdint.h>
#include <stdlib.h>

#define MICROPY_CONFIG_ROM_LEVEL (MICROPY_CONFIG_ROM_LEVEL_EXTRA_FEATURES)

#define MICROPY_ENABLE_COMPILER (1)
#define MICROPY_ENABLE_GC (1)
// The heap grows as code needs it, and is never collected during a call: in WebAssembly, the
// pointers C code holds in locals can't be scanned. Each call starts afresh, as the upstream
// webassembly port's calls do from the top level.
#define MICROPY_GC_SPLIT_HEAP (1)
#define MICROPY_GC_SPLIT_HEAP_AUTO (1)
#define MICROPY_ENABLE_PYSTACK (1)
// Python calls Python without recursing in C, so deep recursion ends in a RuntimeError when the
// pystack runs out, rather than in WebAssembly's own stack overflowing.
#define MICROPY_STACKLESS (1)
#define MICROPY_STACK_CHECK (1)
#define MICROPY_NLR_SETJMP (1)
#define MICROPY_LONGINT_IMPL (MICROPY_LONGINT_IMPL_MPZ)
#define MICROPY_FLOAT_IMPL (MICROPY_FLOAT_IMPL_DOUBLE)
#define MICROPY_USE_INTERNAL_ERRNO (1)
#define MICROPY_USE_INTERNAL_PRINTF (0)
#define MICROPY_ENABLE_SOURCE_LINE (1)
#define MICROPY_ERROR_REPORTING (MICROPY_ERROR_REPORTING_DETAILED)
#define MICROPY_ENABLE_EXTERNAL_IMPORT (0)
#define MICROPY_READER_VFS (0)
#define MICROPY_VFS (0)
#define MICROPY_HELPER_REPL (0)
#define MICROPY_KBD_EXCEPTION (0)
#define MICROPY_ALLOC_PATH_MAX (256)
#define MICROPY_PY_SYS_PLATFORM "spirisynq"
#define MICROPY_PY_BUILTINS_COMPILE (1)
#define MICROPY_PY_BUILTINS_EVAL_EXEC (1)
#define MICROPY_PY_BUILTINS_INPUT (0)
#define MICROPY_PY_BUILTINS_HELP (0)
#define MICROPY_PY_BUILTINS_HELP_MODULES (0)
#define MICROPY_PY_RANDOM_SEED_INIT_FUNC (spirisynq_random_seed())
#define MICROPY_PY_HASHLIB_MD5 (0)
#define MICROPY_PY_HASHLIB_SHA1 (0)
#define MICROPY_PY_HASHLIB_SHA256 (1)
// `json` reads through io.StringIO, so `io` is there too: in-memory streams only.
#define MICROPY_PY_IO (1)

// The modules that aren't promised.
#define MICROPY_PY_ASYNCIO (0)
#define MICROPY_PY_DEFLATE (0)
#define MICROPY_PY_ERRNO (0)
#define MICROPY_PY_FRAMEBUF (0)
#define MICROPY_PY_GC (0)
#define MICROPY_PY_MACHINE (0)
#define MICROPY_PY_MICROPYTHON (0)
#define MICROPY_PY_OS (0)
#define MICROPY_PY_PLATFORM (0)
#define MICROPY_PY_SELECT (0)
#define MICROPY_PY_SYS_STDFILES (0)
#define MICROPY_PY_SYS_STDIO_BUFFER (0)
#define MICROPY_PY_THREAD (0)
#define MICROPY_PY_TIME (0)
#define MICROPY_PY_UCTYPES (0)
#define MICROPY_PY_VFS (0)

#define MP_SSIZE_MAX (0x7fffffff)
typedef long mp_off_t;
#define MP_STATE_PORT MP_STATE_VM
#define MICROPY_HW_BOARD_NAME "SpiriSynq"
#define MICROPY_HW_MCU_NAME "wasm32"

uint32_t spirisynq_random_seed(void);
