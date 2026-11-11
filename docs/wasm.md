# Writing code in WASM

Everything on the previous pages, from properties to callbacks, is plain Python. Plain Python
runs only in Python processes that import your class. The battery's ground station is written in
Rust, and its dashboard in JavaScript: they have the `Battery` type's schema, but not its code.

WASM fixes that. Put a `@wasm` decorator directly under the others, and SpiriSynq ships the code
with the type:

```python
from SpiriSynq import wasm

@dataclass
class Battery(SyncableObject):
    charge_mah: float = 0.0
    capacity_mah: float = 5000.0

    @property
    @wasm.python
    def percent(self) -> float:
        return 100 * self.charge_mah / self.capacity_mah
```

Now the JavaScript dashboard reads `percent` the same way a Python script does, and gets the same
number.

The code runs in WebAssembly, with [Extism](https://extism.org), in every process, Python
included. `@wasm.python` is one of several languages you can write it in; see
[Choosing a language](#choosing-a-language).

## What WASM gives you

What you gain depends only on where the code runs (see
[the table in the overview](overview.md#the-pieces)):

- **Code that runs on mirrors, or wherever it's used, goes in the type's schema.** Every mirror
  gets it with the type and runs it, in any language. That's what `percent` above shows: the
  JavaScript dashboard reads it without anyone writing it in JavaScript. The same goes for
  `before_call` and `after_call`, `before_send` and `after_receive`, and `@on_change.mirror`, so a
  dashboard in any language checks arguments, decodes fields and smooths values the way a Python
  mirror does.
- **Code that runs only on the owner never goes in the schema.** Mirrors don't need it. Writing it
  in WASM makes the owner portable instead: an owner rewritten in another language runs the same
  code.

### A portable owner

Callers can't tell whether a remote method is WASM. The difference is on the owner's side:

```python
    @remote_method
    @wasm.python
    def set_charge_limit(self, percent: int) -> int:
        self.charge_limit_percent = max(50, min(percent, 100))
        return self.charge_limit_percent
```

Next year the battery monitor is going to be firmware written in Rust. A method written in plain
Python would have to be rewritten for it; this one moves with the type.

A WASM method can only touch fields, not hardware. So `set_charge_limit` sets
`charge_limit_percent`, and the owner's own code reacts to that field changing (see
[Signals on `events`](callbacks.md#signals-on-events)). For the same reason, most
[raw remote methods](remote_methods.md#raw-remote-methods) stay plain Python: they usually exist to
talk to hardware.

Use WASM on the owner for the logic that defines how the object behaves, such as rules, limits and
state changes, when it should outlive the language the owner is written in today.

## Rules for WASM code

- **`self` holds the object's fields, and nothing else.** A WASM method can read and set the
  fields declared on the class, and read its WASM properties. It can't see other attributes,
  such as `self._device`, or call plain Python methods.
- **It reaches the network only through fields.** Setting a field is the only effect WASM code
  has outside itself. Owner-side code that needs hardware or the network stays plain Python, and
  reacts to the fields WASM code sets.
- **The function must stand alone.** SpiriSynq ships the function's source, not your module, so
  it can't use variables from around it. It can import only what the bundled interpreter
  provides. SpiriSynq checks this when the class is defined, and raises if the function breaks
  the rule.
- **It always runs in WASM**, in Python processes too, so it behaves the same everywhere.
- **It blocks.** WASM code runs off the event loop, so waiting inside it doesn't hold anything
  else up. See [Async](#async).
- Arguments and return values must be types SpiriSynq can serialize.

### Async

WASM code itself is never async: SpiriSynq runs it off the event loop, and it blocks while it
waits. You can still write it either way:

- **Written with `async def`**, a WASM method stays async to Python callers: calling it gives an
  awaitable, as for any `async def`. When the class is defined, SpiriSynq turns the body into its
  blocking form for WASM: `async def` becomes `def`, `async for` becomes `for`, and `await x`
  becomes `x`.
- **Written with `def`**, it's an ordinary blocking method to Python callers.

So the [hooks on the caller](remote_methods.md#hooks-on-the-caller) can be moved into WASM
unchanged. In WASM, `after_call` runs as a plain generator: `for reply in replies:` waits for each
reply, and each `yield` hands a result to the caller.

```python
    @voltage_history.after_call
    @wasm.python
    async def voltage_history(self, replies: AsyncIterator[Reply]) -> AsyncIterator[list[float]]:
        async for reply in replies:
            yield [round(v, 2) for v in reply.decode()]
```

This is the same hook as on the remote methods page, with `@wasm.python` added. Written with
`def` and a plain `for`, it does the same thing.

Lua and JavaScript write `after_call` with their own generators: a coroutine in Lua, `function*`
in JavaScript.

## Choosing a language

SpiriSynq bundles every runtime below, so every process can run every `@wasm` method, whatever
language the process itself is written in:

| Decorator | Runs as | Start-up | Use it for |
| --- | --- | --- | --- |
| `@wasm.python` | Compiled if it can be, else interpreted | Instant, or about 1 ms | The default: most methods, written in the language the class is in. |
| `@wasm.lua` | Lua 5.4 | Milliseconds | Small scripts, for people who already write Lua. |
| `@wasm.javascript` | QuickJS | Milliseconds | Code shared with a JavaScript dashboard or web app. |
| `@wasm.asm` | WebAssembly text | Instant | Tiny, hot methods, written instruction by instruction. |
| `@wasm.module` | Your compiled module | Instant | Code written in a compiled language, such as Rust, Zig or C. |

All the interpreters together add less than 1 MiB, compressed, to SpiriSynq.

## Python

`@wasm.python` takes an ordinary Python function. SpiriSynq runs it one of two ways:

- **Compiled**, with mamba: SpiriSynq compiles the function to WebAssembly when the class is
  defined. It needs no interpreter, starts instantly and runs fast, but only a typed subset of
  Python compiles.
- **Interpreted**, with MicroPython: SpiriSynq ships the function's source, and runs it in
  MicroPython, a small Python interpreter it bundles. It handles nearly all of Python, with a
  small standard library.

The examples in this section use three more fields on the battery: `serial: str`, such as
`"BAT-2024-00172"`; `firmware: str`, the battery's firmware report as JSON; and
`cell_voltages: list[float]`.

### How the choice is made

`@wasm.python` tries mamba first. If the function compiles, it ships compiled; if not, it ships as
source for MicroPython. Trying costs a few milliseconds, once, when the class is defined. A fully
annotated function that sticks to mamba's subset gets its speed without asking for it.

To choose yourself, use `@wasm.mamba` or `@wasm.micropython` instead:

- `@wasm.mamba` raises when the class is defined if the function doesn't compile, with the reason.
  Use it on a method that must stay fast, so a change that falls out of the subset is caught at
  once, not by a slower dashboard.
- `@wasm.micropython` never tries mamba.

`Battery.percent.wasm_runtime` says which one a method got: `"mamba"` or `"micropython"`.

The two can differ in one way: where an `int` overflows 64 bits, mamba raises, and MicroPython
carries on with a bigger integer, as Python does. A method where that can happen should use
`@wasm.micropython`.

(mamba)=
### Compiled: mamba

The battery's `percent` and `minutes_left`, and a property that adds up the bytes of the serial
number, as a simple checksum:

```python
    @property
    @wasm.mamba
    def percent(self) -> float:
        return 100 * self.charge_mah / self.capacity_mah

    @wasm.mamba
    def minutes_left(self, draw_a: float) -> float:
        return (self.charge_mah / 1000) / draw_a * 60

    @property
    @wasm.mamba
    def serial_checksum(self) -> int:
        total: int = 0
        for b in self.serial.encode():
            total += b
        return total
```

Mamba is ordinary Python, minus everything that needs an interpreter at run time. A mamba
function can use:

- the parameters, annotated, and local variables, annotated where their type isn't obvious from
  the value;
- `int`, `float` and `bool`, with Python's arithmetic and comparisons, and `str`, `bytes` and lists
  of numbers, which it can index, slice, measure with `len` and loop over; `str.encode()` gives a
  string's UTF-8 bytes;
- `if`, `while` and `for`, including `for i in range(...)`, with `break` and `continue`;
- the object's fields, read and set as `self.field`;
- other functions on the class that are also mamba;
- `raise`, with a message, which reaches the caller as an exception.

It can't use classes, closures, `dict`, `try`, imports or the standard library. `int` is a
64-bit integer, and `float` a 64-bit float. An `int` that overflows 64 bits raises, where Python
would carry on.

A construct mamba can't compile, or one where the compiled code would give a different answer from
Python, is refused when the class is defined, with an error that names the line and says why. If
a mamba function compiles, it gives the same answer as the same function run as Python.

With `@wasm.python`, a refused function isn't an error: it runs in MicroPython instead.

### Interpreted: MicroPython

MicroPython runs whatever mamba can't compile: code that uses the standard library, `dict`,
`try`, or no annotations. These need regular expressions, JSON and dictionaries, so they run in
MicroPython, whether they're marked `@wasm.python` or, as here, `@wasm.micropython`:

```python
    @property
    @wasm.micropython
    def manufactured_year(self) -> int:
        import re

        m = re.match(r"BAT-(\d+)-\d+$", self.serial)
        if m is None:
            raise ValueError("serial number not recognised: " + self.serial)
        return int(m.group(1))

    @property
    @wasm.micropython
    def firmware_version(self) -> tuple[int, ...] | None:
        import json

        try:
            info = json.loads(self.firmware)
        except ValueError:
            return None
        return tuple(int(p) for p in info["version"].split("."))

    @wasm.micropython
    def weakest_cells(self, count: int) -> dict[int, float]:
        ranked = sorted(enumerate(self.cell_voltages), key=lambda c: c[1])
        return {i: v for i, v in ranked[:count]}
```

```python
mirror.manufactured_year     # 2024
mirror.firmware_version      # (2, 3, 1)
mirror.weakest_cells(2)      # {3: 3.52, 1: 3.69}
```

Imports go inside the function, because SpiriSynq ships the function, not your module.

#### What MicroPython has

MicroPython implements most of the Python language, and a small standard library:

- These modules are available: `array`, `binascii`, `cmath`, `collections`, `hashlib`, `heapq`,
  `json`, `math`, `random`, `re` and `struct`. Nothing else can be imported.
- Modules are smaller than CPython's. `re`, for example, has no counted repetition: write
  `\d\d\d\d` or `\d+`, not `\d{4}`.
- Some newer syntax and built-in methods are missing, such as `dict | dict`.
- `int` grows as large as it needs to, as in CPython.

The full list is in MicroPython's
[differences from CPython](https://docs.micropython.org/en/latest/genrst/index.html).

A method that runs in MicroPython runs there in Python processes too, never in CPython. So if it
works in your tests, it works the same way on every mirror.

(micropython-version)=
#### Checking the version

SpiriSynq bundles one version of MicroPython, and newer releases of SpiriSynq bundle newer ones. A
method that depends on the version can check `sys.implementation.version`, MicroPython's own
version, and raise or adapt:

```python
    @wasm.micropython
    def summary(self) -> str:
        import sys

        if sys.implementation.version < (1, 27):
            raise RuntimeError("needs MicroPython 1.27 or newer")
        ...
```

## Lua and JavaScript

`@wasm.lua` and `@wasm.javascript` take the function's source as a string. They go on an empty
method, whose signature says what the function takes and returns. The source defines a function
with the method's name:

```python
    @property
    @wasm.lua("""
        function percent(self)
          return 100 * self.charge_mah / self.capacity_mah
        end
    """)
    def percent(self) -> float: ...
```

`self` is a table of the object's fields. The function's other parameters are the method's, in
order:

```python
    @wasm.lua("""
        function minutes_left(self, draw_a)
          return (self.charge_mah / 1000) / draw_a * 60
        end
    """)
    def minutes_left(self, draw_a: float) -> float: ...
```

Assigning to a field of `self` sets the field, as in Python. Here is the mirror callback from
[Reacting to changes](callbacks.md#callbacks-on-mirrors), in Lua:

```python
    @on_change.mirror("current_a")
    @wasm.lua("""
        function smooth_current(self, value)
          self.smoothed_current_a = 0.9 * self.smoothed_current_a + 0.1 * value
        end
    """)
    def smooth_current(self, value: float) -> None: ...
```

How fields look in Lua:

| Python | Lua |
| --- | --- |
| `int` | integer |
| `float` | float |
| `bool` | boolean |
| `str`, `bytes` | string |
| `None` | `nil` |
| `list` | table, indexed from 1 |
| `dict` | table |

`@wasm.javascript` works the same way, with a JavaScript function. `self` is an object, `list` is
an array, `bytes` is a `Uint8Array`, and `int` is a number, or a `BigInt` when it doesn't fit in
one exactly.

```python
    @property
    @wasm.javascript("""
        function percent(self) {
          return 100 * self.charge_mah / self.capacity_mah;
        }
    """)
    def percent(self) -> float: ...
```

For longer code, keep it in a file: `@wasm.lua(file="battery.lua")`. The path is relative to the
file that defines the class.

## WebAssembly text: `@wasm.asm`

`@wasm.asm` lets you write a method in WebAssembly instructions directly, inside a Python
function, the way MicroPython's `@micropython.asm_thumb` lets you write ARM assembly. It goes
anywhere `@wasm.python` does. Like mamba, it needs no interpreter, so it is tiny, starts instantly
and runs fast. Mamba covers most of what you'd write this way; use `@wasm.asm` when you want
control over every instruction.

Here is the battery's `percent` property (see [Methods and properties](methods.md)) in WebAssembly:

```python
    @property
    @wasm.asm
    def percent(self) -> float:
        f64.const(100)
        field.get("charge_mah")
        f64.mul()
        field.get("capacity_mah")
        f64.div()
```

The function body is never run as Python. SpiriSynq reads its source when the class is defined
and turns each line into one WebAssembly instruction, written as in the WebAssembly text format
with Python call syntax: `f64.div` is written `f64.div()`, and `f64.const 100` is written
`f64.const(100)`. The names `field`, `local`, `call`, `i32`, `i64`, `f32` and `f64` don't need
importing; they only exist for SpiriSynq to read.

WebAssembly works on a stack. Each instruction takes its operands off the stack and pushes its
result. Above, `f64.const(100)` and `field.get("charge_mah")` push two numbers, `f64.mul()`
replaces them with their product, and so on. Whatever is left on the stack at the end is the
return value.

### Numbers

`int` is a WebAssembly `i64`, `float` an `f64`, and `bool` an `i32`.

- `field.get("charge_mah")` pushes a field. `field.set("charge_limit_percent")` takes the top of
  the stack and assigns it to the field.
- `local.get("draw_a")` pushes a parameter, by name.
- `local.new("total", f64)` declares a local variable; `local.get` and `local.set` use it.

The battery's `minutes_left` method:

```python
    @wasm.asm
    def minutes_left(self, draw_a: float) -> float:
        field.get("charge_mah")
        f64.const(1000)
        f64.div()
        local.get("draw_a")
        f64.div()
        f64.const(60)
        f64.mul()
```

### Bytes, strings and lists

WebAssembly only has numbers, so anything else lives in memory, the way Extism passes data. For
a `bytes`, `str` or `list` field, `field.get` pushes the address of a block of Extism memory
holding it: the bytes as they are, a string as UTF-8, a list of numbers packed little-endian.
Extism's own functions read and write those blocks, called with `call(...)`:

| Function | Does |
| --- | --- |
| `call("length")` | Takes an address, pushes the block's length in bytes. |
| `call("load_u8")`, `call("load_u64")` | Take an address, push the byte or 64-bit word there. |
| `call("alloc")` | Takes a length, pushes the address of a new block. |
| `call("store_u8")`, `call("store_u64")` | Take an address and a value, and store it. |

To return a `bytes`, `str` or `list`, leave the address of a block on the stack.

Here is mamba's `serial_checksum`, from [Compiled: mamba](#mamba), written instruction by
instruction:

```python
    @property
    @wasm.asm
    def serial_checksum(self) -> int:
        local.new("serial", i64)
        local.new("i", i64)
        local.new("total", i64)

        field.get("serial")       # the address of the string's block
        local.set("serial")

        with block():
            with loop():
                # stop when i reaches the length
                local.get("i")
                local.get("serial")
                call("length")
                i64.ge_u()
                br_if(1)

                # total += the byte at serial + i
                local.get("total")
                local.get("serial")
                local.get("i")
                i64.add()
                call("load_u8")
                i64.extend_i32_u()
                i64.add()
                local.set("total")

                # i += 1, and go round again
                local.get("i")
                i64.const(1)
                i64.add()
                local.set("i")
                br(0)

        local.get("total")
```

`with block():` and `with loop():` are WebAssembly's blocks; `if` is written `with if_():`,
because `if` is a Python keyword. `br(n)` and `br_if(n)` branch as in WebAssembly: out of a
block, or back to the start of a loop. Comments and blank lines are ignored.

SpiriSynq checks the instructions when the class is defined. An unknown instruction, or a stack
that doesn't end with exactly the return value, raises an error that names the line.

## Compiled modules: `@wasm.module`

`@wasm.module(file="battery.wasm", export="balance")` runs a function from a WebAssembly module you
compiled yourself, on an empty method like the Lua and JavaScript ones. The path is relative to the
file that defines the class.

A module compiled from Rust, Zig, Go, C or any language with an Extism PDK is an ordinary Extism
plugin. Its input is `self` and the arguments, as JSON matching the type's schema. It can also
call SpiriSynq's host functions to read or set a single field or argument by name, without
decoding the whole input: useful when `self` has a large `bytes` field.

## How the code travels

Each WASM method is an entry in the type's schema. The entry names the *loader* that runs it, the
function to call, and the code itself:

| Loader | Code | Written with |
| --- | --- | --- |
| `python` | Python source, for MicroPython | `@wasm.micropython`, or `@wasm.python` when mamba can't compile it |
| `lua` | Lua source | `@wasm.lua` |
| `javascript` | JavaScript source | `@wasm.javascript` |
| `wat` | WebAssembly text | `@wasm.asm` (translated to WebAssembly text when the class is defined) |
| `wasm` | A compiled WebAssembly module, base64-encoded | `@wasm.module`; `@wasm.mamba`, and `@wasm.python` when mamba compiles it (compiled when the class is defined) |

The loaders are built into SpiriSynq, so every process can run every entry.

Each entry is identified by a hash of its contents. Small code is included in the schema. Larger
code is fetched from the object's owner the first time a mirror needs it.

### Interpreter versions

SpiriSynq bundles one version of each interpreter, and newer releases of SpiriSynq bundle newer
ones. A method may run in a newer interpreter than the one it was written with. Code that depends
on the version checks it itself, and raises or adapts; for MicroPython, see
[Checking the version](#micropython-version).
