"""`@wasm.*`: code that travels with its type and runs in WebAssembly, in every process.

```python
@property
@wasm.python
def percent(self) -> float:
    return 100 * self.charge_mah / self.capacity_mah
```

Each decorator turns a function into a `WasmFunction`. The class's schema carries it as an
`x-wasm` entry (see `rust/ARCHITECTURE.md`, "Code entries and loaders"), and calling it runs it in
the core, never in CPython, so it behaves the same everywhere.
"""

from __future__ import annotations

import ast
import builtins
import functools
import hashlib
import inspect
import os
import symtable
import textwrap
from collections.abc import Callable
from typing import Any

PYTHON, LUA, JAVASCRIPT, WAT, WASM = "python", "lua", "javascript", "wat", "wasm"

_GLOBALS_IN_WASM = set(dir(builtins)) | {"RpcException"}


class WasmFunction:
    """A function whose code runs in WebAssembly. Call it like the method it stands for."""

    def __init__(self, func: Callable, loader: str, source: str | bytes, entry: str):
        functools.update_wrapper(self, func)
        self.func = func
        self.loader = loader
        self.source = source
        self.entry = entry
        self.name = func.__name__
        self.is_async = inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)

    @property
    def wasm_runtime(self) -> str:
        """What runs it: `"micropython"`, `"lua"`, `"quickjs"`, `"wat"` or `"module"`."""
        return {
            PYTHON: "micropython",
            LUA: "lua",
            JAVASCRIPT: "quickjs",
            WAT: "wat",
            WASM: "module",
        }[self.loader]

    @property
    def hash(self) -> str:
        data = self.source.encode() if isinstance(self.source, str) else self.source
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def entry_schema(self, builder, role: str, of=None) -> dict:
        """This code's `x-wasm` entry."""
        from SpiriSynq.values import hints_of

        entry: dict = {"role": role}
        if of is not None:
            entry["of"] = of[0] if len(of) == 1 else list(of)
        entry.update(
            loader=self.loader,
            entry=self.entry,
            source=self.source,
            hash=self.hash,
        )
        if role in ("property", "method"):
            entry["params"] = builder.parameters(self.func, 1)
            hint = hints_of(self.func).get("return")
            if hint is not None:
                entry["returns"] = builder.resolve(hint)
        return entry

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return functools.partial(self, obj)

    def __call__(self, obj, *args, **kwargs):
        if self.is_async:
            return obj._synq_run_wasm_async(self, args, kwargs)
        return obj._synq_run_wasm(self, args, kwargs)

    def __repr__(self) -> str:
        return f"<{self.wasm_runtime} code {self.name}>"


# --- Python -------------------------------------------------------------------------------


class _Blocking(ast.NodeTransformer):
    """`async def` → `def`, `async for` → `for`, `async with` → `with`, `await x` → `x`; and
    annotations dropped, since they name things that only exist in CPython."""

    def visit_AsyncFunctionDef(self, node):
        self.generic_visit(node)
        made = ast.FunctionDef(**{f: getattr(node, f) for f in node._fields})
        return ast.copy_location(made, node)

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        node.returns = None
        return node

    def visit_AsyncFor(self, node):
        self.generic_visit(node)
        return ast.copy_location(ast.For(**{f: getattr(node, f) for f in node._fields}), node)

    def visit_AsyncWith(self, node):
        self.generic_visit(node)
        return ast.copy_location(ast.With(**{f: getattr(node, f) for f in node._fields}), node)

    def visit_Await(self, node):
        self.generic_visit(node)
        return node.value

    def visit_arg(self, node):
        node.annotation = None
        return node

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        if node.value is None:
            return None
        return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)


def python_source(func: Callable) -> str:
    """`func`'s source as MicroPython runs it: standing alone, without its decorators, in its
    blocking form. `TypeError` if it uses names from around it."""
    try:
        text = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError) as e:
        raise TypeError(f"{func.__qualname__}: can't read its source for WASM: {e}") from None
    tree = ast.parse(text)
    fn = tree.body[0]
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise TypeError(f"{func.__qualname__}: only a function can be WASM code")
    fn.decorator_list = []
    tree = ast.fix_missing_locations(_Blocking().visit(tree))
    source = ast.unparse(tree) + "\n"
    _check_stands_alone(func, source)
    return source


def _check_stands_alone(func: Callable, source: str) -> None:
    table = symtable.symtable(source, "<wasm>", "exec").get_children()[0]
    missing: set[str] = set()

    def walk(t):
        for symbol in t.get_symbols():
            name = symbol.get_name()
            if symbol.is_global() and symbol.is_referenced() and name not in _GLOBALS_IN_WASM:
                missing.add(name)
        for child in t.get_children():
            walk(child)

    walk(table)
    if missing:
        names = ", ".join(sorted(missing))
        raise TypeError(
            f"{func.__qualname__}: WASM code must stand alone, but it uses {names} from around "
            f"it. Import what it needs inside the function, or pass it in."
        )


def python(func: Callable) -> WasmFunction:
    """Python, run in the bundled MicroPython."""
    return WasmFunction(func, PYTHON, python_source(func), func.__name__)


micropython = python
"""Python, run in the bundled MicroPython: the same as `python` in this release."""


def mamba(func: Callable) -> WasmFunction:
    """Compiled Python. Not in this release: the compiler isn't built yet."""
    raise NotImplementedError(
        f"{func.__qualname__}: @wasm.mamba isn't in this release; use @wasm.micropython"
    )


def asm(func: Callable) -> WasmFunction:
    """WebAssembly written as Python calls. Not in this release: use `@wasm.wat`."""
    raise NotImplementedError(
        f"{func.__qualname__}: @wasm.asm isn't in this release; write WebAssembly text with "
        f"@wasm.wat"
    )


# --- other languages ----------------------------------------------------------------------


def _read(func: Callable, file: str, binary: bool) -> str | bytes:
    base = os.path.dirname(inspect.getsourcefile(func) or func.__code__.co_filename)
    path = os.path.join(base, file)
    with open(path, "rb" if binary else "r", encoding=None if binary else "utf-8") as f:
        return f.read()


def _source_decorator(loader: str, binary: bool = False):
    def decorator(source: str | bytes | None = None, *, file: str | None = None,
                  export: str | None = None):  # fmt: skip
        if (source is None) == (file is None):
            raise TypeError(f"@wasm.{loader}: give the code, or file=, not both")

        def wrap(func: Callable) -> WasmFunction:
            code = source if file is None else _read(func, file, binary)
            if isinstance(code, str) and not binary:
                code = textwrap.dedent(code)
            return WasmFunction(func, loader, code, export or func.__name__)

        return wrap

    return decorator


lua = _source_decorator(LUA)
"""Lua 5.4 source, defining a function with the method's name: `@wasm.lua("...")` or
`@wasm.lua(file="battery.lua")`, on an empty method whose signature says what it takes."""

javascript = _source_decorator(JAVASCRIPT)
"""JavaScript source for the bundled QuickJS, as for `lua`."""

wat = _source_decorator(WAT)
"""WebAssembly text, a module exporting the method's name (or `export=`)."""


def module(source: bytes | None = None, *, file: str | None = None, export: str | None = None):
    """A compiled WebAssembly module (an Extism plug-in): `@wasm.module(file="x.wasm",
    export="balance")`."""
    return _source_decorator(WASM, binary=True)(source, file=file, export=export)


# --- the schema's entries -----------------------------------------------------------------


def entries(cls: type, builder) -> dict:
    """The `x-wasm` entries of a syncable class."""
    from SpiriSynq.callbacks import OnChange
    from SpiriSynq.remote_callables import RemoteMethod
    from SpiriSynq.schema import members

    fields = {f.name for f in cls.synq_fields()}
    out: dict[str, dict] = {}
    for name, member in members(cls):
        if isinstance(member, property) and isinstance(member.fget, WasmFunction):
            out[name] = member.fget.entry_schema(builder, "property")
        elif isinstance(member, WasmFunction):
            hook = _field_hook(name, fields)
            if hook is not None:
                field, role = hook
                out[name] = member.entry_schema(builder, role, [cls.synq_key_of(field)])
            else:
                out[name] = member.entry_schema(builder, "method")
        elif isinstance(member, RemoteMethod):
            if isinstance(member.func, WasmFunction):
                raise NotImplementedError(
                    f"{cls.__qualname__}.{name}: WASM remote methods run on the owner, which "
                    f"isn't in this release"
                )
            for role in ("before_call", "after_call"):
                hook = getattr(member, f"{role}_func")
                if isinstance(hook, WasmFunction):
                    out[f"{member.name}.{role}"] = hook.entry_schema(builder, role, [member.name])
        elif isinstance(member, OnChange) and isinstance(member.func, WasmFunction):
            if member.side == "owner":
                raise NotImplementedError(
                    f"{cls.__qualname__}.{name}: WASM @on_change.owner runs on the owner, which "
                    f"isn't in this release"
                )
            keys = [cls.synq_key_of(f) for f in member.fields]
            out[name] = member.func.entry_schema(builder, "on_change_mirror", keys)
    return dict(sorted(out.items()))


def _field_hook(name: str, fields: set[str]) -> tuple[str, str] | None:
    for suffix in ("before_send", "after_receive"):
        field = name.removesuffix(f"_{suffix}")
        if field != name and field in fields:
            return field, suffix
    return None


def call_args(func: Callable, args: tuple, kwargs: dict) -> dict[str, Any]:
    """The arguments of a call to `func` (without `self`), by name, defaults filled in."""
    sig = inspect.signature(func)
    bound = sig.bind(None, *args, **kwargs)
    bound.apply_defaults()
    return dict(list(bound.arguments.items())[1:])
