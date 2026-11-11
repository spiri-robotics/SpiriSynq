# SpiriSynq v2: Rust core architecture

Status: decisions from the architecture pass of 2026-11-11. Nothing here is implemented yet.

SpiriSynq v2 moves the protocol into a Rust core. Python becomes a binding on top of it (PyO3). v2
may break both the wire protocol and the Python API.

## JSON Schema is the shared language

The Rust core and its language bindings communicate about types **only** through JSON Schema
(2020-12, with SpiriSynq's `x-` keywords). A binding hands the core the schema of each object
type. The core works out the object's keys from that schema and declares its zenoh resources
accordingly. It never inspects the binding's native types.

### Type identity (`$id`)

Every type schema carries an `$id`. In principle it can be any URI. v2 supports two forms:

- **PyPI as the source of truth** for types defined in a Python package:
  `urn:pypi:<distribution>:<version>:<import path>`, e.g.
  `urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera`. The import path is the one you would
  use to import the class. Unpublished code (editable installs, dev builds, local changes) still
  uses this form, with whatever version it reports.
- **Baked-in UUID** for types not on PyPI, or with no source of truth: the author puts
  `<uuid>:<version>` in the class.

Other sources of truth (e.g. crates.io for Rust-defined types) may come later. v2 starts with no
Rust-defined types.

There are no evolution rules. Keeping a type backwards compatible is up to its implementors.
Version mismatches between nodes are out of scope: at most, the library reports them.

## Subscriptions come from the type tree

- Subscriptions are exact keys, worked out statically from the type. There are no `<path>/**`
  subscriptions. An object only subscribes to keys it knows how to handle.
- **Mirrors** subscribe to every path in their type tree.
- **Authoritative objects** also subscribe to their paths, so that anyone can write to a field
  (same as v1: a put on `<path>/<field>`). A per-field setting turns off incoming writes on the
  authoritative object. It is the receive-side counterpart of `synq_skip_sync`; the name is not
  chosen yet (e.g. `synq_ignore_incoming`).
- **Lazy publish** only works on fields where incoming writes are turned off. Zenoh's matching
  status can't exclude our own subscriber: measured with zenoh 1.9, even a subscriber declared
  with `allowed_origin=REMOTE` counts as a match. This is an accepted limitation.

## Remote method keys

- A remote method answers on `<topic>/<method>` by default. A key template gives it part of the
  key tree instead: `@remote_method(key="read/data/{path:**}")`. The schema records it as the
  method's `x-key`.
- `{name}` matches one chunk; `{name:**}` matches the rest (zero or more chunks) and must come
  last. Each placeholder is a `str` parameter of the method. The owner declares a queryable on the
  template with placeholders replaced by `*` / `**`, and passes the matched chunks as arguments.
- The point is Zenoh ACLs: putting the variable part in the key lets router ACLs do per-path
  access control. Group operations by kind before the variable part (`read/…`, `write/…`).
- Values are percent-encoded for `*`, `$`, `?`, `#` and `%`, in the core, so every language does
  it the same way. Empty chunks (leading, trailing or doubled `/`) raise `ValueError` on the
  caller.
- No two members of a class may answer overlapping keys (methods or fields; `**` matches zero
  chunks). Checked at class definition.

## Liveliness

- Each authoritative object declares a zenoh liveliness token. The token says whether the object
  is alive. It replaces the v1 tombstone.
- Mirrors **do nothing** when a token disappears or reappears. They only report it.
- Every nested child object has its own token.
- The lease is zenoh's default and is left to the deployment's zenoh config. Measured with the
  default config: a killed process's token disappears immediately (its TCP connection closes); a
  frozen process's token disappears after about 9 s.
- Mirrors don't declare tokens. Owners learn about mirrors from publisher matching status.
- Liveliness queries complement `sr_metadata` for discovery; they don't replace it.

## Nested objects are links

v1 duplicates nested objects. A child keeps its own address, and every change to it is published
twice: on its own key and again under its parent's key. The parent's `sr_rehydrate` and schema also
inline the child's state and address. v2 only removes that duplication:

- A field that holds a syncable object is a **link** on the wire: `!Link <topic>`. The parent's
  schema and `sr_rehydrate` contain only the link. The child is described by its own schema and
  served by its own `sr_rehydrate`.
- An object, a mirror and a link are interchangeable in a field. `foo.bar = Bar()` and
  `foo.bar = Link("some/topic")` are treated the same way, and both go on the wire as a link.
- A link can point anywhere. A child's default topic is derived from its parent
  (`<parent>/<field>`). How a child learns its parent when it's created isn't solved yet; it may be
  done by hand at first.
- Children outlive their parents: closing a parent leaves its children open.
- Reassigning `foo.bar` drops the old object. Python garbage-collects it, which closes it, and the
  new object takes its place.
- By default, mirrors follow links: they mirror the target and re-point when the link changes.
  Following can be turned off.
- Collections of children (`list[Camera]`, `dict[str, Camera]`) are collections of links.
- A link to an object of the wrong type counts as bad incoming data, and is handled like any other.
- A link to a topic no one serves gives an object-not-found placeholder, which can be bound again
  later.

## Validation and escape hatches

- The core must support escape hatches and fall back to plain zenoh: publish and receive hooks,
  custom framing, payloads that aren't YAML. So the core does only limited validation: it checks
  key routing. Decoding and checking values is the binding's job.
- Python reaches these escape hatches through **SpiriSynq's own wrapper types**. It doesn't use
  `eclipse-zenoh` types: those belong to a separate zenoh runtime and can't wrap the core's session.
  Users are meant to control most of this. The wrappers will grow gradually, alongside a better
  system for managing per-field settings.

## WASM methods (Extism)

WASM gives objects behaviour that works on every platform. It is run with Extism. WASM isn't a
kind of method: every code slot on an object takes either plain Python or a `@wasm.*` decorator.
Slots are classified by trigger and by where they run, with one vocabulary (owner / mirror):

| Trigger | Wherever used | Owner | Mirror |
| --- | --- | --- | --- |
| Read | `@property` | | |
| Called | plain method | | |
| Remote call | | `@remote_method`, `@remote_method_raw` | `@m.before_call`, `@m.after_call` |
| Field changes | | `@on_change.owner` | `@on_change.mirror` |

- Local slots (property, method) run on whichever copy uses them, on that copy's fields; a method
  setting a field on a mirror is an ordinary mirror write.
- `@on_change.owner`: runs whenever the field changes, however it changed; its writes publish.
- `@on_change.mirror`: runs on each mirror when a change arrives; its writes change only that
  mirror's copy, are never published, and an owner value later replaces them.
- Setting the watched field inside its own callback doesn't re-trigger it.
- Everything on the call path is async, matching zenoh's model (a get yields a stream of replies).
- Caller hooks on a remote method, no raw variants: `async before_call(call, *args)` checks the
  args and sends the get exactly once, via `await call.send_args(dict)` (usual encoding) or
  `await call.send(payload, parameters, encoding, attachment)` (raw); raising cancels, not sending
  is a `RuntimeError`. `after_call(replies)` is an async generator over the successful replies
  (error replies raise `RpcException` from the iteration) and yields results, via `reply.decode()`
  (usual) or `reply.payload` / `.encoding` / `.attachment` (raw); it may join or split replies. A
  non-generator method must get exactly one yielded value. Both optional and WASM-able (in the
  schema when WASM); skipped when the owner calls its own method. WASM needs host functions for
  `send_args`, `send`, `decode`, pulling the next reply and yielding a result.
- `@remote_method_raw`: an async generator on the owner; gets the raw `Query` (plus args decoded
  the usual way, if it declares any) and yields `Reply`s, each one zenoh reply
  (`Reply(payload, encoding=, attachment=)` raw, `Reply.encode(value)` usual); raising sends an
  error reply. WASM-able (the query is passed as data: payload, parameters,
  attachment, key; replies are yielded like `after_call`'s results), owner only, never in the
  schema; calling it on the owner raises `TypeError`. Schema
  result type comes from `after_call`'s yield type, else raw bytes. For per-method wire control on the owner.
- Plain `@remote_method`s may be `async def` or async generators on the owner; each yielded value
  is one zenoh reply.
- A remote method parameter annotated `Query` gets the incoming query (`None` for an owner's own
  call) and is left out of the schema. Replaces the dropped `handler` hook for auth/metadata.
- **Call chaining (promise pipelining, after Cap'n Proto).** Explicit: `m.pipe(...)` on a remote
  method whose return type is a syncable type returns a pending link at once and sends the call
  eagerly (so side effects happen even if the result is dropped). A plain call still waits and
  returns a mirror; each further step is pipelined with its own `.pipe(...)`. Remote calls and field reads on a pending
  link are pipelined to the owner without waiting; a field read pipelines the object's rehydrate
  and turns the pending link into a mirror. The owner runs each step on the object the previous
  step returned; only the last step's replies return. If the object is owned elsewhere, the owner
  makes the remaining calls itself under its own zenoh identity (accepted: ACLs check the owner);
  if it can't reach it, it hands back the topic and the caller continues (one extra round trip).
  Caller hooks: every `before_call` runs at call time; only the last step's `after_call` runs.
  Errors name the failing step; later steps don't run. Plain-value and raw methods end a chain.
  Wire: to be designed. Eager sending means the owner keeps each call's result object under a
  caller-chosen call id until pipelined calls referencing it have arrived (bounded by the
  method's timeout).
- WASM code sees `self` as the schema describes it (fields only, never native internals such as
  `__dict__`), plus its declared parameters.

**Schema rule:** WASM code that runs on mirrors or wherever used is in the JSON schema. Owner-only
code never is; WASM there makes the owner portable to another language instead.

A module is identified by its hash. Small modules are inlined into the schema. Larger ones are
served by an `sr_*` callable that returns the code. The Rust library bundles a few interpreters compiled to WASM (e.g. for JavaScript or Python), so a
method can ship as source code for one of them instead of as a compiled module.

### Code entries and loaders

Everything is built on a generic loader. Each WASM method is one entry in the schema:

```yaml
x-wasm:
  cell_voltage:
    role: property        # property | method | before_call | after_call | on_change_mirror
    loader: python        # python | lua | javascript | wat | wasm
    entry: cell_voltage   # function to call inside the code
    source: |             # omitted when large; fetched by hash
      ...
    hash: sha256:...
    params: {...}         # JSON Schema
    returns: {...}
```

- Loaders are baked into the core: `python` (MicroPython), `lua` (Lua 5.4) and `javascript`
  (QuickJS, via Javy) run source text in a bundled interpreter; `wat` (WebAssembly text, assembled
  with the `wat` crate); `wasm` (a compiled binary, base64). Compiled languages ship their raw
  binary format, never converted to text. `@wasm.asm` translates to WAT and ships as `wat`.
  Inline source is a string argument (`@wasm.lua("""...""")`), or `file=`.
- `@wasm.mamba`: a typed Python subset compiled to WASM at class definition, shipped as `wasm`.
  The compiler lives in the core, based on the `waspy` crate (MIT; Python AST → IR →
  `wasm-encoder`, Binaryen optional). Needs changing: `int` is i64 for us (waspy uses i32), fields
  via our host functions, overflow raises. Refuses anything it can't compile with CPython's answer.
- WASM code is blocking, never async: the host runs it off the event loop. Source written with
  `async def` is accepted: the Python-facing method stays awaitable, and the shipped body is
  rewritten at class definition (`async def`→`def`, `async for`→`for`, `await x`→`x`). In WASM,
  `after_call` runs as a plain generator; `for reply in replies` blocks on a host function.
- `@wasm.python` tries mamba at class definition (milliseconds) and falls back to MicroPython;
  `@wasm.mamba` (raises if it can't compile) and `@wasm.micropython` choose explicitly.
  `<method>.wasm_runtime` reports the choice. Known divergence: int overflow (mamba raises,
  MicroPython has bigints).
- Measured 2026-11-11 (wasmtime 50): MicroPython WASI 362 KiB (160 KiB gz), ~1 ms start; Javy's
  QuickJS plugin 1.3 MiB (494 KiB gz). MicroPython has no async generators (async for works).
- Interpreters fetched by hash on first use (e.g. a CPython + scientific-stack build, ~84 MiB
  download, 240 MiB compiled, 20 s first compile) come later, on a content-addressable store
  still to be designed. Not bundled.
- Loader names are unversioned. We can't bundle every interpreter version forever: the core bundles
  one per loader. Code that depends on the version checks it itself (`sys.version_info`) and raises
or adapts.
- Input is Extism's: `self` and the arguments as JSON matching the schema. SpiriSynq also provides
  host functions (`field_get_*`, `field_set_*`, `arg_get_*`) to every loader, so a module can read
  or set one field or argument by name without decoding the whole input.

## Still open

- How an object is told its parent when it's created (and so its derived topic).
- Codecs were dropped (2026-11-11). Custom encoding is per field: `<field>_before_send(value,
  sample)` sends once via `sample.send(raw…)` or `sample.send_value(v)`; `<field>_after_receive(
  sample)` returns the value (`sample.decode()` for the usual way). Methods named by convention,
  like settings; WASM-able and in the schema (both run on mirrors). Full-state fetch carries each
  field as before_send sent it. The settings pattern itself is decided: a
  class attribute `<field>_<setting>` set to a preset, e.g. `exposure_qos = qos.realtime`,
  `recording_reliability = qos.reliable` (default `qos.drop`, best effort), `voltage_ignore_incoming = True`,
  `image_lazy = True`. Settings are fixed at class definition, never changed at run time.
- Packaging: maturin, platform wheels, and the CI workflows the project template owns.
