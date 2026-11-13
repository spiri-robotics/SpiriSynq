# SpiriSynq v2: Rust core architecture

Status: decisions from the architecture pass of 2026-11-11. Implemented so far: `spirisynq-yaml`,
`spirisynq-schema`, `spirisynq-core`, `spirisynq-zenoh`, the extension module of
`spirisynq-python` (the Python API isn't rebuilt on it yet), and `spirisynq-extism` with every
loader: `wat`, `wasm`, and the bundled MicroPython, Lua and QuickJS (WASM properties, methods,
field hooks and caller hooks).

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

### Schema format

Implemented in `spirisynq-schema` (2026-11-11). A type schema is a JSON Schema 2020-12 object
schema, carried as YAML. The core reads the keywords below; everything else passes through to
the bindings, which check values against it.

```yaml
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3        # format version; v1 wrote 2
$id: urn:pypi:spirirobot:1.2.0:spirirobot.Robot
x-bases: [urn:pypi:spirirobot:1.2.0:spirirobot.Vehicle]   # syncable base types
properties:                  # one per synced field (synq_skip_sync fields are left out)
  voltage: {type: number}
  image: {type: string, x-yaml-tag: "!!binary", x-skip-rehydrate: true}
  gps: {anyOf: [{$ref: "#/$defs/Gps"}, {type: "null"}]}
  camera: {type: string, x-yaml-tag: "!Link", x-link: "urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera"}
$defs:
  Gps: {type: object, x-sync: fields, properties: {latitude: {type: number}}}
x-remote-methods:
  read:
    x-key: read/data/{path:**}
    parameters: {type: object, properties: {path: {type: string}}}
    returns: {type: string}  # the schema of each reply
    x-generator: true        # any number of replies; without it, exactly one
x-wasm: {...}                # see "Code entries and loaders"
```

- **`$id` forms:**
  - `urn:pypi:<dist>:<version>:<import path>`;
  - `urn:uuid:<uuid>:<version>`, written for a class that sets `synq_type_id = "<uuid>:<version>"`;
  - any other absolute URI, which is opaque and has no version.

  Type identity ignores the version. Distribution names are compared after PEP 503
  normalization, and UUIDs ignore case.
- **Field keys:** each property `f` is published on `<topic>/f`. A `$defs` entry marked
  `x-sync: fields` is a `SubSyncableDataclass`. A field that refers to one, directly or in an
  `anyOf` branch, also gets a key for each of that dataclass's fields: `<topic>/f/latitude`,
  recursively. Such a dataclass can't contain itself, and a field can refer to at most one.
  Other dataclasses are sent whole.
- **Links:** `x-link` names the type a link points to. It can sit anywhere in a field's schema:
  in lists, in dicts, in union branches, or in fields of a plain dataclass. A link's target
  must be that type, or list it in `x-bases`.
- **Remote method keys:**
  - A remote method answers on its name, or on `x-key`. Placeholders must be string parameters.
  - A `{name}` value also escapes `/`, as `%2F`.
  - Decoding accepts only the escapes the encoder writes, so each value has exactly one key. An
    ACL on `read/data/secret` can't be sidestepped with `read/data/%73ecret`.
- **Reserved keys:** a member's key may not start with a chunk beginning `sr_`, or with a
  placeholder. No two members' keys may overlap. Both are checked when the schema is loaded.
- **Forward compatibility:** unknown `x-wasm` roles and loaders are kept, so a schema from a
  newer peer still loads; the core just can't run that code.

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

- Each authoritative object declares a zenoh liveliness token,
  `<topic>/sr_alive/<zid of the owner's session>`. The token says whether the object is alive,
  and which session owns it. It replaces the v1 tombstone.
- Mirrors **do nothing** when a token disappears or reappears. They only report it. One
  exception (decided 2026-11-11): until a state fetch completes, a mirror fetches again,
  in the core, whenever an owner's token appears, including one that appears during a fetch
  that then ends incomplete. A mirror opened before its owner, or while the owner's
  declarations are still on their way (both starting together), catches up once the owner is
  reachable. Each fetch still ends with `Rehydrated`, so an incomplete one is reported.
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
  (`<parent>/<field>`). Assigning the child to the parent's field sets its parent, as in v1
  (decided 2026-11-11).
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
                          #   | before_send | after_receive
    of: ...               # hooks only: the remote method (before/after_call) or field key;
                          #   an on_change_mirror may list several
    loader: python        # python | lua | javascript | wat | wasm
    entry: cell_voltage   # function to call inside the code
    source: |             # omitted when large; fetched by hash. `wasm` takes !!binary
      ...
    hash: sha256:...      # of the UTF-8 text or the module bytes; checked when inline
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
  or set one field or argument by name without decoding the whole input. See "Running code".

### Running code

Built for properties and methods, with every loader (2026-11-12).

- The engine doesn't run WASM: a `WasmHost` does (`spirisynq-extism` natively, the browser's own
  engine later). `Session::wasm_job` takes a snapshot of the object's fields into a `Frame`; the
  host runs the code against the frame, holding no lock on the engine; `Session::finish_wasm`
  sets the fields the code set, as `set_field` does (an owner publishes, a mirror writes to the
  owner), and reads the result. If the code fails, nothing is set.
- The entry function's input is `{"self": {...fields}, "args": {...}}`; its output is its
  result as JSON, empty for `null`. Code sees values as the schema describes them: tags are
  dropped and bytes are base64 strings. Fields without a value, and fields with their own
  encoding, are left out of `self`; code can't read or set the latter.
- The result and the fields code sets are read back guided by their schemas: a string under
  `x-yaml-tag: "!!binary"` becomes bytes; a string or collection under a local tag such as
  `!Link` becomes that tagged value (a `null` stays `null`); a whole number under `type: number`
  (not `integer`) becomes a float, since JavaScript can't write `3.0`. In an `anyOf`, the first branch
  whose `type` fits guides the value. `$ref`s into `$defs` are followed; a schema that refers to
  itself stops guiding after 64 steps instead of looping.
- Host functions, imported from `extism:host/user`. Names and JSON text are Extism memory
  blocks (an `i64` offset), as Extism passes strings:

  | Function | Does |
  | --- | --- |
  | `field_get(name) -> json` | A field's value, `null` if it has none. `name` is its key: `voltage`, `gps/latitude`. |
  | `field_get_i64(name) -> i64`, `field_get_f64(name) -> f64` | The same, for an integer or a number. |
  | `field_set(name, json)`, `field_set_i64(name, i64)`, `field_set_f64(name, f64)` | Set a field; later reads see it. A property can't. |
  | `arg_get(name) -> json`, `arg_get_i64`, `arg_get_f64` | An argument's value. |
  | `sample_payload() -> bytes`, `sample_encoding() -> text`, `sample_attachment() -> bytes` | In an `after_receive`: the sample that arrived. No attachment is block 0. |
  | `sample_decode() -> json` | In an `after_receive`: the sample decoded the usual way, from YAML. |
  | `sample_send(payload, encoding, attachment)` | In a `before_send`: send the value as these bytes (`attachment` 0 for none). |
  | `sample_send_value(json)` | In a `before_send`: send a value the usual way, as YAML. |
  | `call_send_args(json)` | In a `before_call`: send the call with these arguments, a mapping by name, the usual way. |
  | `call_send(payload, parameters, encoding, attachment)` | In a `before_call`: send the call as these bytes, with these selector parameters (0 for the caller's). |
  | `reply_next() -> json` | In an `after_call`: wait for the next reply. `{"reply": <index>}`, `{"error": {"kind", "message"}}`, or `null` once the call has ended. |
  | `reply_payload(i64) -> bytes`, `reply_encoding`, `reply_attachment`, `reply_decode` | In an `after_call`: a reply it has had, by index, as for a sample. |
  | `reply_escaped()` | In an `after_call`, about to fail: the error reply it was handed is why. |
  | `call_yield(json)` | In an `after_call`: yield a result. |

  Block 0 is empty bytes (Extism gives an empty block offset 0), so an empty attachment is no
  attachment.

  A host function that can't do what it's asked (no such field, not an integer, a property
  setting a field, a `before_send` sending twice) stops the code with an error saying why.
- Field hooks (2026-11-12) run the same way, through `Session::on_change_job`,
  `after_receive_job` and `before_send_job`, and the same `finish_wasm`; `run_on_change`,
  `run_after_receive` and `run_before_send` do all three. The binding calls them when their
  moment comes, as it applies values: the engine doesn't run code by itself.
  - `on_change_mirror(self, value)` runs on a mirror only, and gets `field` too when it watches
    several. Its writes are applied with `set_field_local`, never published. Several may watch
    one field; `run_on_change` runs them in the schema's order, each seeing what the ones
    before it set, and stops at the first that fails.
  - `after_receive(self, sample)` returns the field's value, read against the field's schema;
    the binding applies it. It can't set fields.
  - `before_send(self, value, sample)` must send exactly once; finishing publishes what it sent,
    as `send_raw` does. It can't set fields.
  - A field with a WASM `before_send` or `after_receive` has its own encoding (`raw`), whatever
    the binding's options say, so every binding hands its samples to the hook. A field or
    remote method has at most one hook of each other kind; the schema rejects a second.
- Caller hooks (2026-11-12) run on the caller, around a call to a remote method.
  - `before_call(self, call, *args)` gets the caller's arguments, in the order of its `params`,
    and sends the call exactly once: `send_args` the usual way, or `send` as it is (`payload`,
    `parameters`, `encoding`, `attachment`). The before_call's arguments named by the key
    template still fill the key; parameters of its own replace the caller's. Raising, or not
    sending, sends nothing. `Session::before_call_job` gives a `CallJob`, `finish_call` makes
    the call and gives its `RequestId`; `run_call` does it all, or makes a plain `call` for a
    method without one. It can't set fields.
  - `after_call(self, replies)` is a generator over the call's replies. The binding gives the
    job a `Replies` (`Session::after_call_job`): the code waits in `Replies::next` for each
    reply, so it runs on a thread of its own while the binding feeds the call's
    `Notice::Reply`s in. A reply reads like an incoming sample; an error reply is raised in the
    code as `RpcException` (`kind`, `message`), and if it gets out uncaught, finishing gives
    `Error::Reply` with it, for the caller to raise as it is. A generator method's results go
    to `Replies::give` as they're yielded; for a method that replies with one value,
    `finish_wasm` gives the one result, and yielding none or several is an error. It can't set
    fields.
  - A method with a WASM `after_call` always has its replies raw (`CallOptions::raw_replies`).
  - Extism's time limit counts the wall clock, so `spirisynq-extism` keeps its own: a watcher
    thread per host cancels the plug-in when its time is up, and the clock stops while an
    `after_call` waits for a reply. The wait itself is bounded by the call's timeout. In the
    browser, a host function can't wait on the main thread: its binding will need a worker, or
    JavaScript Promise Integration.
- Sandbox: WASI with no files, no environment and no network; the code's stdout and stderr go
  nowhere. Each call gets 1 s and 64 MiB by default (`spirisynq_extism::Limits`). Modules are
  compiled once per host (an interpreter once for all the code it runs), and **every call gets a
  fresh instance**: nothing code keeps in globals survives to the next call, and a trap leaves
  nothing behind. wasmtime's on-disk compile cache is off. WebAssembly exceptions are on
  (Extism's `wasmtime-exceptions`), for the interpreters' setjmp/longjmp.

### The bundled interpreters

- `python` is MicroPython 1.29.0, `lua` is Lua 5.4.9, `javascript` is QuickJS-ng 0.18.0. Each is
  an Extism plug-in exporting `run`, with a small C runner of ours
  (`rust/spirisynq-extism/interpreters/`). Its input is `{"source", "entry", "args": [...]}`,
  the arguments in the order of the entry's `params` (as given, without `params`). It runs the
  source as a module (named `code`), calls `entry(self, *args)`, and outputs the result as JSON.
  `self` reads and sets fields through `field_get` and `field_set` as it's used. A hook's
  arguments go in the order the engine gives them (`params` is ignored), and an `after_receive`
  or `before_send` gets a sample object last (`"sample": "in"` or `"out"` in the input). Coming
  in it has `payload` (bytes), `encoding`, `attachment` (bytes or none) and `decode()`; going
  out, `send(payload, encoding, attachment)` (encoding `zenoh/bytes` by default) and
  `send_value(value)`, written the language's way: `sample.send(p, encoding=…, attachment=…)`
  in Python, `sample:send(p, encoding, attachment)` in Lua, `sample.send(p, {encoding,
  attachment})` and `sample.sendValue(v)` in JavaScript. Payloads are `bytes` in Python, strings
  in Lua and `Uint8Array`s in JavaScript (an `ArrayBuffer` or other view is taken too).
- A `before_call` gets a call object first, before its arguments (`"call": "send"`):
  `call.send_args(args)`, and `call.send(p, parameters=…, encoding=…, attachment=…)` in Python,
  `call:send(p, parameters, encoding, attachment)` in Lua, `call.sendArgs(args)` and
  `call.send(p, {parameters, encoding, attachment})` in JavaScript. An `after_call` gets the
  replies as its one argument (`"call": "replies"`), each reply read like an incoming sample,
  and is driven as the language's generator: a generator function in Python, a coroutine
  (`coroutine.yield`) in Lua, a `function*` in JavaScript. `for reply in replies` in Python,
  `for reply in replies do` in Lua, `for (const reply of replies)` in JavaScript. An error
  reply raises `RpcException` (`kind`, `message`; a global in Python and JavaScript), or in Lua
  a table with `kind` and `message`.
- Sizes: MicroPython 289 KiB (122 KiB gzipped), Lua 244 KiB (99), QuickJS 963 KiB (348): 570
  KiB gzipped together, under the 1 MiB the docs promise. Measured in release builds: the first
  call compiles the interpreter (90 ms Lua, 155 ms MicroPython, 350 ms QuickJS, once per host);
  after that a call takes 0.3 to 0.6 ms, fresh instance included.
- What each has: Lua opens base, coroutine, table, string, math and utf8, and `load` takes text
  only (Lua's bytecode loader trusts its input). QuickJS has no libc, so no I/O at all.
  MicroPython has the modules the docs list plus `io` (in-memory streams; `json` needs it), no
  files and no external imports. It is stackless, so deep recursion raises `RuntimeError`.
- MicroPython never collects garbage during a call: pointers held in WebAssembly locals can't
  be scanned. Its heap grows instead (split heap), as in the upstream webassembly port. So
  everything one call allocates must fit in the call's memory limit; a fresh instance per call
  frees it all.
- Values: JSON, with these rules. Lua: integers and floats as written; a table is an array when
  its keys are exactly 1..n, and an empty table is an empty array. JavaScript: an integer a
  number can't hold exactly arrives as a `BigInt`, and a `BigInt` goes out exactly. Python:
  integers of any size, and `bytes` going out as base64; a `Uint8Array` too, in JavaScript. Everywhere, a non-finite float or a
  function is an error, not `null`, and an integer key goes out as a string.
- Errors carry the language's own type and message, and the line in `code` where they can:
  `ValueError: too high (line 2)`, `code:1: too high`, `RangeError: too high (code:2:9)`.
- Built by `interpreters/default.nix` from pinned sources and pinned tools (nixpkgs' wasm32-wasi
  clang and wasi-libc, binaryen's `wasm-opt -Oz`), and committed. `make interpreters` rebuilds
  them; the build is reproducible, and the `Rust` workflow fails if a rebuild differs from what
  is committed. The C is ours or upstream's, not Rust: it isn't in the coverage report, and runs
  only inside the WebAssembly sandbox, where a bug can give wrong results but can't reach the
  host. The interpreter tests (`spirisynq-extism/tests/`) exercise it.

Not yet:
- Code fetched by hash.
- In the interpreters, `bytes` fields arrive as base64 strings: the docs promise Lua strings,
  `Uint8Array`s and `bytes`. The runners need the field's schema to decode them.
- Integer dict keys come back as strings (`{3: 3.52}` returns `{"3": 3.52}`), until results are
  read against their `returns` schema's key types.

## Language targets

Decided 2026-11-11: **Python, C++ and browser JavaScript.** Node isn't a target.

- Python: PyO3 + maturin, hand-written (the decorator/descriptor surface needs it).
- C++: a C ABI (cbindgen header) with a header-only C++ wrapper over it, as zenoh-cpp does.
- Browser JS: the browser runs the same Rust core, compiled to wasm32, and speaks **real zenoh
  over a WebSocket**, decoded by a Rust zenoh implementation in the browser (decided 2026-11-11).
  No zenoh-ts, no remote-api plugin: SourceInfo, liveliness and the rest are ordinary zenoh.
  Native zenoh has a `ws/` link, so any native peer (drone, ground station) can listen for
  browsers directly. The core can't assume Tokio or threads. WASM methods sit behind a host
  abstraction (wasmtime natively, the browser's own engine in the browser).
- C++ and browser users only mirror types defined in Python, for now. Defining their own types
  comes later.
- YAML is parsed and emitted in the Rust core, never by the binding, so every language reads the
  wire the same way (no third-party C++ YAML parsers). Bindings receive a parsed value tree, YAML
  tags included, and build their native values from it.
- The wire is a restricted subset of YAML 1.2 (decided 2026-11-11). The core's parser rejects
  anything outside it, and its writer emits one canonical style that always reads back:
  - Scalars use YAML 1.2's JSON schema: only `true`, `false`, `null` and JSON-style numbers are
    typed, plus `.inf`, `-.inf` and `.nan`; everything else is a string (`on`, `yes`, `0755`,
    `1_000` are strings). The writer quotes any string that would read as something else.
  - Banned: anchors and aliases, directives, multiple documents, non-scalar mapping keys,
    duplicate keys, anything that isn't UTF-8.
  - Local tags (`!Link`, `!RootFrame`, any registered type's tag) are allowed; `bytes` travel as
    `!!binary` (base64). The core rejects every other `!!` tag (e.g. `!!python/object/apply`)
    and verbatim `!<...>` tags. A local tag no type has registered passes through to the binding
    as a tagged value; the binding treats it as bad data if nothing claims it.
  - Change detection treats NaN as equal to NaN, so a field set to NaN again doesn't republish.
    `spirisynq_yaml::Value` equality is "would be written as the same text": floats compare by
    bits except NaN, so `0.0` and `-0.0` differ.
  - A plain scalar with no text (`key:`) is rejected rather than read as null; write `null`.
  - Integers must fit an `i64`; floats too large to be finite (`1e999`) are rejected.
  - Nesting is limited to 128 levels, so hostile input can't overflow the parser's stack.
  - Implemented in `spirisynq-yaml` (2026-11-11).

- The browser runs **zenoh-nostd** (decided 2026-11-11). Main zenoh (0764376, 2026-11-09) has
  no wasm support. zenoh-nostd (Eclipse; 9e12ae8, 2026-10-27; "early development") already runs
  in the browser as a client over `ws/`, but its API has only put/subscribe/get/queryable: no
  liveliness, no matching status, the Interest protocol isn't implemented, and samples expose
  only key and payload (its protocol crate does encode SourceInfo and attachments). We
  contribute those upstream; browser mirrors can ship with fields and remote calls first.
- `zenoh-link-ws` listens on plain `ws` only, so pages served over https (which must use `wss`)
  need TLS terminated in front of the peer, or `wss` added upstream.
- Previously considered (zenoh-ts over `zenoh-plugin-remote-api`): it carries no SourceInfo and
  its WebSocket protocol is crate-private. Rejected in favour of real zenoh.

## Wire protocol

Decided 2026-11-11, implemented in `spirisynq-core`. Every payload the core writes or decodes is
YAML (encoding `application/yaml`); everything else passes through to the binding untouched.

| Key under `<topic>` | Who | What |
| --- | --- | --- |
| `<field>`, `<field>/<sub-field>`… | owner and mirrors | puts of field values |
| `<method key>` | owner's queryable | remote calls |
| `sr_rehydrate` | owner's queryable | full state |
| `sr_metadata`, `sr_metadata/<type>` | owner's queryable | `{topic, owner: <zid>, type: <$id>, bases: [<$id>…]}` |
| `sr_object_schema`, `sr_type_schema/<type>` | owner's queryable | the type's schema |
| `sr_alive/<zid>` | owner's liveliness token | the owner is alive, in that session |

`<type>` is a type id without its version, as one key chunk (`pypi:<dist>:<path>`,
`uuid:<uuid>`, `uri:<escaped URI>`), mounted for the type and each of its bases, so
`**/sr_metadata/<type>` finds every object of a type, whatever its version.

- **Fields: the owner confirms.** A mirror applies only values its owner published. A write from
  a mirror is a put on the field's key; the owner applies it (or ignores it, with
  `ignore_incoming`), runs its callbacks, and republishes the result, so every mirror converges
  on the owner's value. The writing mirror shows its own value at once, until the owner's
  arrives. The owner is the session named by the newest liveliness token, or, before one
  arrives, the session that answered the state fetch.
- **Sources** are zenoh `SourceInfo`: a mirror checks the session id; an owner drops its own
  echoes by publisher. A sample without `SourceInfo` is never taken as the owner's. `SourceInfo`
  is self-reported, so this is consistency, not security: ACLs on the field keys are.
- **Full state** (`sr_rehydrate`): one reply per field, keyed `<topic>/<field>` and exactly as
  the field's put would be (payload, encoding, attachment), so custom encodings and shared memory
  work for it too. Fields with `x-skip-rehydrate` and fields never set are left out. The get
  accepts replies on any key. A mirror subscribes before it fetches; a value that arrives live
  during the fetch wins over the fetched one for that key.
- **Remote calls:** a get on the method's key. Arguments not in the key template are one YAML
  mapping in the query payload; selector parameters are left to the caller (the raw escape
  hatch, such as an auth token). Each value is one reply. A successful answer ends with a reply of
  encoding `x-spirisynq/done` and an empty payload, on the same key; a stream that ends without
  it was cut short (timeout, owner gone). An error is an error reply
  `{type: <exception type>, message: <text>}`, and ends the call. The built-in queries end
  with the done reply too. Consolidation is always off.
- **Wildcards stand for the topic part only** (decided 2026-11-11). A query reaches a member
  (a remote method or a built-in query) when it ends with that member's key written out and
  the object's key for it matches the query: `**/set_limit` calls `set_limit` on every object
  that has it, `fleet/*/battery/set_limit` on every battery under `fleet`, `**/sr_metadata`
  asks every object. `**` or `robot/**` reaches nothing, and `**/set_limit` doesn't call
  `read(path="set_limit")`, since `read`'s part isn't written out. Each owner answers on its
  own concrete key. Gathering the answers of a fan-out call is left to a `before_call` hook.

## Crate layout

A Cargo workspace in `rust/`. Lower crates know nothing about the ones above them.

| Crate | What it is | Runs in |
| --- | --- | --- |
| `spirisynq-yaml` | The strict YAML 1.2 subset: parser and canonical writer, producing a `Value` tree that keeps tags. Built on `saphyr-parser`'s event stream; the subset rules are ours. | everywhere |
| `spirisynq-schema` | The JSON Schema model we use, `$id`, `x-wasm` entries, and compiling a type tree into its key plan (the exact keys an object publishes and subscribes to). | everywhere |
| `spirisynq-core` | The engine: authoritative objects and mirrors, field state, change detection, echo suppression, the built-in queries, remote-call routing, liveliness and listener reports. Sans-IO: it emits `Command`s for a transport to carry out and takes the network's `Event`s back, so it has no runtime, no threads and no clock, and runs unchanged in the browser. `memory::MemoryNetwork` connects sessions in one process for tests. WASM runs through a `WasmHost` trait (see "Running code"). | everywhere |
| `spirisynq-zenoh` | The transport over main zenoh (1.10), with the `shared-memory` and `unstable` features (`SourceInfo` needs `unstable` anyway). A `Node` owns a zenoh session and an engine; see "The zenoh transport" below. | native |
| `spirisynq-extism` | `WasmHost` over Extism 1.41 (wasmtime 49), with the bundled interpreters (see "The bundled interpreters"). Needs Rust 1.96 or newer, so the whole workspace does. | native |
| `spirisynq-python` | PyO3 extension module `SpiriSynq._core`; the Python package in `src/SpiriSynq/` builds the user API on it. See "The Python extension module" below. | native |
| `spirisynq-c` | The C ABI and its cbindgen header. The header-only C++ wrapper lives beside it in `rust/spirisynq-c/cpp/`. | native |
| `spirisynq-web` | The browser build: `Transport` over zenoh-nostd's `ws/` link, `WasmHost` over the browser's own WebAssembly engine, and the JS API (wasm-bindgen). | browser |

- **Shared memory** (zenoh's experimental SHM transport), between processes on one machine,
  measured with zenoh 1.10:
  - **Zero-copy** when the payload is written into a shared-memory buffer in the first place:
    `Node::alloc_shm(len)` gives one, sent as a `ZenohBytes` payload (a raw field, a raw call or
    a raw reply). The receiver maps the same memory; nothing goes through the socket.
  - **One copy** otherwise: zenoh copies a message of 3 KiB or more into shared memory as it
    sends it (`transport/shared_memory/transport_optimization`, on by default). The provider
    starts on first use (`mode: lazy`), so the first large message goes the usual way;
    `mode: init` starts it with the session, at the cost of its 16 MiB pool in every process.
  - Payloads are `spirisynq_core::Payload`, a shared handle the core never copies. A payload
    that arrives is zenoh's own buffer, shared memory included, readable in place, and goes
    back out as the same buffer. Field values the core encodes as YAML are copied once into
    zenoh; they're small.
  - zenoh 1.10 bug, worked around in `alloc_shm`: `.wait()` on the session's provider while it
    is still starting takes the one message saying it's ready, so the session never records it
    and frees it, and every buffer from it, with the last handle ("Buffer is invalidated" on
    the receiver). It also stops implicit shared memory for the session. `alloc_shm` polls
    instead. Worth reporting upstream.
- Field values inside the core are `spirisynq_yaml::Value`. Each binding converts them to and
  from native values; none of them parses YAML.
- The core reports what happened (field changed, link re-pointed, token gone, call arrived) as
  events. Each binding delivers them its own way: Python posts them to the asyncio loop, C calls a
  function pointer, JS resolves promises and fires callbacks.
- Native bindings run the core on one driver thread owned by `spirisynq-zenoh` (zenoh brings
  its own runtime; no Tokio of ours); the browser runs it on the page's event loop
  (`wasm-bindgen-futures`).
- Python packaging moves from hatchling to maturin (mixed Rust/Python project:
  `python-source = "src"`, manifest `rust/spirisynq-python/Cargo.toml`). Decided 2026-11-11:
  changed here in the project itself, no template variant; `uvx copier update` carries the local
  change through template updates.
- **v2 replaces v1** (decided 2026-11-11): `src/SpiriSynq/` is rebuilt on the core in place, not
  kept beside it. v1 and v2 peers can't talk to each other.

### The Python extension module

- `SpiriSynq._core` is thin and private: a `Node` per zenoh session, whose methods are the
  engine's (`create_owner`, `set_field`, `call`, `reply`…). `run_wasm(object, name, args)`
  runs a WASM property or method on the node's Extism host, without the GIL. Objects and requests are opaque
  `ObjectId`s and `RequestId`s. The user API in `src/SpiriSynq/` is built on it in Python.
- A type's WASM hooks run inside `_core` (2026-11-13), so the Python layer never drives
  them: `next_notice` decodes with an `after_receive`, `set_field` sends with a `before_send`
  (every time: the engine can't compare a raw field's values), `set_field_local` runs the
  `on_change_mirror`s when the value changed, and `call` makes the call with a `before_call`
  (`call_raw` skips it). A call with an `after_call` gets a thread of its own: `next_notice`
  feeds it the call's replies and hands out what it yields instead, as `Reply`s, then the
  call's end. An error reply it lets through is a `Reply` with that error; code that fails is a
  `Reply` with error kind `SynqError`. Closing the node ends the `after_call`s still waiting.
- Code runs on a pool of Extism hosts, one per piece of code running at once, each compiling
  what it runs on first use. `Node(config, wasm_timeout=1.0, wasm_memory_mib=64)` sets the
  limits.
- Notices come from `Node.next_notice(timeout)`, one class per kind (`Incoming`, `Call`,
  `Reply`…, and `SchemaFetched` for a schema fetch). It waits without the GIL and wakes every
  100 ms so Ctrl-C works. The engine never calls into Python, so the driver thread never needs
  the GIL. The Python layer decides how to dispatch notices (asyncio, a thread).
- Field values convert directly between `Value` and Python: `None`, `bool`, `int` (64-bit),
  `float`, `str`, `bytes`, lists (tuples go out as lists), dicts, and `_core.Tagged(tag,
  value)` for local tags such as `!Link`. Python never parses YAML. Values nested more than
  `MAX_DEPTH` deep are refused, which also stops self-referencing lists.
- Raw payloads (`_core.Sample`) are copied once into and out of Python. Zero-copy, including
  shared memory, would need Python's buffer protocol, which PyO3 only offers as `unsafe`. Left
  for when it's needed; it would go in the unsafe table below.
- No `unsafe`: PyO3's macros work under `#![forbid(unsafe_code)]`.
- Packaging: maturin builds the wheel (abi3, Python 3.13 and up). `uv sync` rebuilds the
  extension when Rust sources change (`tool.uv.cache-keys`). Platform wheels for PyPI need
  the publish workflow to build per platform; not done yet.

### The zenoh transport

- zenoh's callbacks only queue what arrived. One driver thread hands it to the engine and
  carries out the engine's commands, so the engine is never called from inside zenoh. The
  binding works the engine through `Node::with(|session| …)` (commands go out when it returns)
  and takes notices from a channel.
- Every put carries `SourceInfo` (the publisher's id and a sequence number); zenoh doesn't add
  it by itself. A sample or reply from this session is matched to the local publisher or
  queryable by entity id.
- Gets go to every matching queryable (`QueryTarget::All`). zenoh reports a timeout as an
  error reply with payload `Timeout`, either from the getter's own session (no replier) or from
  a routing node (entity 0); the transport drops it, so the engine sees the get end without
  its done marker and reports it cut short.
- QoS: a reliable field is sent reliably with congestion control `Block`; others best effort,
  `Drop`. Priorities map one to one.
- A failed zenoh operation is logged (`log`) and the engine carries on; a failed get still ends.

### Testing

Every crate keeps 100% line and function coverage, checked by `make rust-test` (which the `Rust`
workflow runs). The extension module is measured by its Python tests (`tests/core`): the target
runs the cargo tests and pytest against an instrumented build under one `cargo llvm-cov` session,
then `rust/coverage_gate.py` checks the merged report. The gate leaves out only PyO3's generated
`__pymethod_*` wrappers and the `#[pymethods]`/`#[pyclass]` lines they sit on: llvm-cov counts
the wrappers of methods that take no arguments as never called even when they are (PyO3 0.30).
The methods themselves are counted. Region coverage
is reported but not enforced: some regions are error paths that can't occur, such as a `?` on a
call that can't fail in that context. Defensive branches that can't be reached go on a line that
is reached, rather than behind an exclusion; unit tests inside a crate don't count towards its
coverage, because cargo-llvm-cov measures the library and its unit-test build separately. Keep
library types non-generic where tests use them: each test binary builds its own copy of a
generic function, and a copy one binary never calls counts as missed even when another binary
runs it.

### Unsafe code

Every crate starts with `#![forbid(unsafe_code)]`, so the compiler refuses any `unsafe`. A crate
that needs it switches to `#![deny(unsafe_code)]` and allows it only on the specific items, each
with a `// SAFETY:` comment saying why it holds. Every such item is listed here, with why it's
needed, and the maintainer is told when one is added. Unsafe inside dependencies (zenoh, PyO3,
wasmtime, wasm-bindgen) isn't ours and isn't listed.

| Where | Why | Status |
| --- | --- | --- |
| `spirisynq-c`: every exported `extern "C"` function | A C API receives raw pointers (strings, buffers, handles) from C/C++ code; Rust can't check them, so reading them is `unsafe` by definition. Each function checks for null and documents what the caller must guarantee. Unavoidable for a C API. | expected, not written yet |

Build order: `yaml` → `schema` → `core` with an in-memory `Transport` for tests → `zenoh` →
`python` → `extism` → `c` → `web` (`extism` moved up on 2026-11-12, so Python's `@wasm.*`
methods can be tested end to end). Each is tested before the next starts.

## Still open


- Following links is the binding's job for now: the core carries `!Link` values like any other.
  Moving it into the core, so every binding follows links the same way, comes with the C and
  browser bindings.
- A mirror's state fetch and a live value for a sub-field: a live `gps/latitude` doesn't stop a
  fetched `gps` from replacing it with the older value. Rare (it needs the change to land
  between the mirror's subscription and the owner's answer), and the next change fixes it.

- Codecs were dropped (2026-11-11). Custom encoding is per field: `<field>_before_send(value,
  sample)` sends once via `sample.send(raw…)` or `sample.send_value(v)`; `<field>_after_receive(
  sample)` returns the value (`sample.decode()` for the usual way). Methods named by convention,
  like settings; WASM-able and in the schema (both run on mirrors). Full-state fetch carries each
  field as before_send sent it. The settings pattern itself is decided: a
  class attribute `<field>_<setting>` set to a preset, e.g. `exposure_qos = qos.realtime`,
  `recording_reliability = qos.reliable` (default `qos.drop`, best effort), `voltage_ignore_incoming = True`,
  `image_lazy = True`. Settings are fixed at class definition, never changed at run time.
- Packaging: maturin, platform wheels, and the CI workflows the project template owns.
