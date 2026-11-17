# Changelog

## Unreleased

### Features

- **`spirisynq-yaml`, the first Rust crate of the v2 core**: the strict YAML 1.2 subset the wire
  uses, with one parser and one canonical writer for every language. Scalars are typed by YAML
  1.2's JSON schema plus `.inf`/`-.inf`/`.nan`, so `on`, `yes` and `0755` are strings. Anchors,
  aliases, `---`, multiple documents, collection keys, duplicate keys, empty values and every
  tag except local ones and `!!binary` are rejected.

- **`spirisynq-schema`**: reads a type's JSON Schema and works out its key plan, the exact keys
  each field and remote method uses. That includes the per-field keys of nested dataclasses,
  key templates with percent-encoding that has only one encoding per value, and the link
  targets each field may hold. It also reads type ids (`urn:pypi:`, `urn:uuid:` or any URI;
  identity ignores the version) and `x-wasm` entries, checking inline code against its hash. It
  rejects schemas whose members answer overlapping keys, or that claim the `sr_` keys
  SpiriSynq reserves. It also checks topics, and gives each type id a versionless key chunk for
  discovery.

- **`spirisynq-core`, the engine**: authoritative objects and mirrors, field publishing with
  change detection, writes from mirrors confirmed by the owner, echo suppression, nested
  dataclasses synced field by field, custom (raw) encodings, lazy fields and listener reports,
  liveliness, full-state fetches, remote calls with key templates, raw methods and raw replies,
  discovery and schema fetches. A mirror opened before its owner, or while the owner is still
  starting, fetches the owner's state again once the owner's token appears. Wildcards in a query
  stand for the topic part only: `**/set_limit` calls every object's `set_limit`, each owner
  answering on its own key, while `**` alone reaches nothing. It does no I/O, so it runs the same over zenoh, in the browser
  and in tests; payloads pass through it uncopied, ready for zenoh's shared-memory transport.
  The v2 wire protocol is in `rust/ARCHITECTURE.md`.

- **`spirisynq-zenoh`, the engine over zenoh**: a `Node` owns a zenoh session and an engine,
  with one driver thread between them. Puts carry `SourceInfo` for echo suppression and owner
  checks; zenoh's timeout replies end a call as cut short rather than failed. Shared memory is
  compiled in: large messages go through it with one copy, and `Node::alloc_shm` gives a buffer
  to write into for zero-copy, working around a zenoh 1.10 bug that freed such buffers in
  flight.

- **`SpiriSynq._core`, the engine in Python** (`spirisynq-python`): a PyO3 extension module
  with a `Node` per zenoh session, the engine's methods, and its notices as Python classes.
  Field values convert straight to Python values, with no YAML on the Python side. The
  package is now built with maturin instead of hatchling. Building it from source needs a
  Rust toolchain. The Python API isn't rebuilt on it yet. v2 replaces v1 in place.

- **WASM properties and methods** (`spirisynq-extism`): a type's `x-wasm` code runs on Extism,
  sandboxed and limited to 1 s and 64 MiB per call, with WebAssembly text (`wat`) or compiled
  modules (`wasm`). Code gets the object's fields and its arguments as JSON, and reads and sets
  single fields through host functions. What a method sets is published as `set_field` would,
  and only once it succeeds; a property can't set fields. In Python, `_core.Node.run_wasm`.
  Building the Rust core now needs Rust 1.96 or newer.

- **Python, Lua and JavaScript code in WASM**: the `python`, `lua` and `javascript` loaders run
  source in bundled interpreters, MicroPython 1.29, Lua 5.4.9 and QuickJS-ng 0.18, compiled to
  WebAssembly (572 KiB gzipped together). The entry function is called with `self` first; `self`
  reads and sets fields. Every call starts from a fresh instance, in about half a millisecond.
  The interpreters are built from pinned sources with Nix (`make interpreters`), reproducibly,
  and committed; CI checks a rebuild matches.

- **Field hooks in WASM**: `on_change_mirror`, `after_receive` and `before_send` run from a
  type's `x-wasm` in every loader. An `on_change_mirror` changes only that mirror's copy, and
  may watch several fields, given the one that changed. An `after_receive` gets the sample
  (`payload`, `encoding`, `attachment`, `decode()`) and returns the field's value; a
  `before_send` gets the value and sends it exactly once, raw with `send` or the usual way with
  `send_value`. A field with either has its own encoding in every process. A field or remote
  method can have only one hook of each other kind.

- **Caller hooks in WASM**: `before_call` and `after_call` run from a type's `x-wasm` in every
  loader. A `before_call` gets the caller's arguments and sends the call exactly once, with
  `send_args` or raw with `send`; raising sends nothing. An `after_call` is a generator over the
  call's replies, which reach it as they arrive, and yields what the caller gets; an error reply
  is raised in the code as `RpcException`, and reaches the caller as it is if the code doesn't
  catch it. A method with an `after_call` always has its replies raw. The time limit on code no
  longer counts time spent waiting for a reply.

- **`SpiriSynq._core` runs the WASM hooks by itself**: field and caller hooks run as values and
  calls pass through the `Node`, and an `after_call`'s results arrive as the call's `Reply`
  notices. `Node(config, wasm_timeout=…, wasm_memory_mib=…)` sets the time and memory limits on
  code.

- **Bytes are bytes in code**: a `bytes` value reaches Python code as `bytes`, JavaScript as a
  `Uint8Array` and Lua as a string, in a field, an argument, a decoded sample or reply, typed or
  not, and the language's bytes go back out as bytes. A string from Lua is its own bytes where
  the schema says bytes; one that isn't UTF-8 is bytes anywhere. Text where an `anyOf` allows
  bytes or text stays text.

- **Integer keys stay integers**: a `dict[int, …]` field's schema says `x-key-type: integer`, with
  `propertyNames: {pattern: "^-?[0-9]+$"}` for validators that don't know that keyword. WASM code
  gets integer keys as integers in Python and Lua (JavaScript's keys are strings), and what it
  hands back has integer keys where it used them or the schema says so, so `weakest_cells` in
  the WASM docs returns `{3: 3.52, 1: 3.69}` as promised. Code compiled to WebAssembly learns
  where bytes and integer keys are in its JSON from the new `marks` host function and the
  input's `"marks"`, and marks its own with `mark`.

### Tests

- **The Rust core must keep 100% line and function coverage.** `make rust-test` and the new
  `Rust` workflow fail below it. `spirisynq-yaml` has 58 tests, including property tests that
  everything written reads back equal and that the parser never panics on arbitrary input.
  `spirisynq-schema` has 47, including property tests that every key built from a template
  decodes back to the same arguments. `spirisynq-core` has 88, run over an in-memory network
  that connects several sessions as zenoh would. `spirisynq-zenoh` has 21, over real zenoh
  sessions on loopback TCP, shared memory and zenoh failures included. `spirisynq-extism` has 34:
  a WebAssembly plug-in that uses every host function, past its time and memory limits
  included, and each interpreter's values, bytes, integer keys, errors, sandbox, field hooks and caller hooks, the docs'
  MicroPython examples among them. `SpiriSynq._core` has 52 Python tests in `tests/core`, which also measure the
  extension module's Rust coverage.

### Internal

- **The flake builds the Rust side too**: `packages.<system>.interpreters` builds the bundled
  interpreters, and `nix develop .#rust` gives the toolchain `make rust-test` needs (Rust 1.99,
  cargo-llvm-cov, LLVM 23). Its nixpkgs pin moves to nixos-unstable of 2026-11-11.

- **The docs specify SpiriSynq v2**, a Rust core with Python as a binding over it, written ahead of
  the code as the specification it has to meet. They cover objects and field settings, links,
  liveliness, methods and remote methods (key templates, hooks, raw methods, call chaining),
  callbacks, WASM (MicroPython, mamba, Lua, JavaScript, inline WebAssembly) and type identity.
  The v1 docs move to `docs-v1/` for reference and are no longer built. `rust/ARCHITECTURE.md`
  records the design decisions behind them.

- **Follows the Spiri project template** (SpiriProjectTemplates v0.5.0): the docs are a
  controlled document published to GitHub Pages with a PDF per release, CI lints, tests and
  builds the docs, and the PyPI workflow is now `pypi-publish.yml`. Lint findings in the
  existing code are not fixed yet.

## v0.6.0

### Features

- **Points: a `Pose` without an orientation.** `Pose(offset, orientation=None)` is something
  with a place but no facing, such as a region of interest, instead of one that falsely
  faces along its parent's axes. A point child composes to a point; under a point, a child at
  a zero offset is a point at the same place, and any other child's pose is None, since a
  point has no axes to offset along. The default orientation is still the identity.

## v0.5.0

### Features

- **Tab completion from a background server.** `synq completion bash|zsh|fish` prints a
  completion script. It completes topics one chunk at a time (including `./`), type tags,
  `topic call` methods, and their `key=value` arguments, with values taken from each
  parameter's type hint (enum/`Literal` members, `true`/`false`, `null`, the default). The
  script talks to `synq _complete-server`, a process that keeps a zenoh session open and
  exits after 10 idle minutes. Without it, Tabs complete topics and tags from
  `~/.cache/spirisynq/topics`. This replaces Typer's `--install-completion` and
  `--show-completion`; a script installed that way keeps working like the fallback.
- **`python -m SpiriSynq`** runs the CLI.
- **`Link`, a reference to another object.** `SpiriSynq.link.Link("myhost/camera")` (or
  `Link.to(obj)`) is sent as `!Link myhost/camera` and described in schemas as a string with
  `x-yaml-tag: '!Link'`. Every session decodes it without registration. Links must be
  absolute topics. `link.resolve(Camera)` fetches a mirror of the target.

- **`Position` is one `pose` value.** `offset`/`orientation`/`accuracy` and the three
  `mount_*` fields are replaced by `mount: Pose` (what you set, in the parent's frame) and
  `pose: Pose` (the root-frame result), each published as one put. Children no longer
  combine a new offset with a stale orientation, and each parent update costs a child one
  recompute instead of three. Breaking for code using the old fields; `fuse_positions`
  skips positions whose pose is unknown.
- **Geodesy for `Position`.** `Offset.from_geodetic(lat, lon, height)` / `.to_geodetic()`
  convert WGS84 to and from ECEF, and `Orientation.ned_to_ecef(lat, lon)` places a local NED
  frame. Body frames are FRD; the module docstring states the conventions.

### Fixes

- **`Position` with an unknown parent.** It used to compose with the identity and publish
  a pose near the root frame's origin; `pose` is now None until the parent's pose is known,
  and goes back to None when the parent is deleted.
- **`Position` under a quiet parent.** A child only heard the parent's later updates, so a
  parent that published once (an EKF origin, say) never placed it. The child now asks the
  parent's `sr_rehydrate` for its current pose. `relative_to` also accepts `./` topics now.
- **Only the authoritative `Position` computes `pose`.** Mirrors followed the parent too,
  and wrote the result back as their own puts.
- **Subclasses registered after their base.** A subclass inherited its base's `yaml_tag`
  and its base's cached `valid_sync_paths` / field types, so once the base had been used,
  the subclass serialized as the base and lost its own fields on the wire. Tags and caches
  are now per class. `sr_metadata`'s type tags also named an unregistered intermediate class
  by its base's tag (`!MavRobot, !Position, !Position`, no `!Robot`); each class now reports
  its own.
- **Private fields and type registration.** `register_type_recursive` walked `_` fields'
  types too, so a field like `_lock: threading.Lock` failed at `sync()`. It now skips them,
  as `valid_sync_paths` and `to_yaml` already did.
- **`load_untyped` on tagged scalars.** A tagged scalar or sequence such as
  `!RootFrame ECEF` raised a `ConstructorError`; it now loads as the plain value.

### Improvements

- **Protocol spec: permissions.** `docs/protocol.md` now says that write access is enforced
  by Zenoh's router-level access control, not by SpiriSynq, and that two authoritative
  nodes on one topic is undefined behaviour.

## v0.4.0

### Fixes

- **`;` in an RPC argument.** A `;` inside an argument value split the Zenoh selector
  parameter, and the call got no reply until it timed out. Argument values are now
  percent-encoded for `%` and `;` (client, `synq topic call`) and percent-decoded by the
  server; `decode_parameters(query)` does the same for custom `.server()` hooks. This is a
  wire change: a value containing a literal `%XX` sequence from an older client is now
  decoded. A request whose parameters fail to parse now gets `reply_err`, not silence.

## v0.3.0

### Breaking

- **Object schemas are now standard JSON Schema (draft 2020-12), format version 2.** Every
  `sr_object_schema` reply carries `$schema` and `x-spirisynq-schema: 2` and passes the
  2020-12 meta-schema. Changes from the old format:
  - `Literal` becomes `enum`, `X | None` adds `null` to the type, `tuple` uses
    `prefixItems`, sets use `uniqueItems`, `bytes` uses `contentEncoding: base64`, Enum
    classes become `enum` of their values, and unknown classes become `{x-python-type}`
    instead of a made-up `type` name.
  - `required` now lists every field (rehydrate always sends them all). Properties carry
    their `default`.
  - `synq_topic` and `synq_base_topic` are no longer in `properties`.
  - New wire keywords: `x-yaml-tag`, `x-encoding` (codec-encoded fields such as `bytes`
    → `zenoh/bytes`), `x-classes`, `title`.
  - `x-rpc-endpoints`: `parameters` is now a JSON Schema object with `required` and
    `additionalProperties: false`, `returns` is always present (`{type: 'null'}` for
    `-> None`), and generator methods carry `x-generator: true` plus `yields`. The built-in
    `sr_*` callables describe themselves accurately (`sr_rehydrate` returns `{$ref: '#'}`).
  - Docstrings are cleaned with `inspect.cleandoc`, and `@dataclass` auto-docstrings are
    dropped.
- **`synq meta type_schema` prints `{topic, schema}` records**, one per answering object,
  separated by `---`, instead of bare schemas.

### Fixes

- **`sr_type_schema` is served again.** It was lost in the April session refactor, so
  `synq meta type_schema` always reported "No schema found". It's the `sr_object_schema`
  callable mounted at `<topic>/sr_type_schema/<Tag>` for each tag in the class's MRO, the
  same way `sr_metadata/<Tag>` works, so `**/sr_type_schema/<Tag>` gets one reply per object
  of that type, keyed by the object's path.
- **`sr_rehydrate` answered every query twice.** Its `.client()` hook exposed the same
  method under a second attribute, and `sync()` declared a queryable for each. The schema
  also listed a nonexistent `sr_rehydrate_client` endpoint. Both are fixed.
- **The tombstone came from a second publisher.** It had its own entity ID and `source_sn`
  counter, so receivers couldn't order it against the object's field puts or tell whether
  they'd missed updates before it. Each object now has exactly one publisher, declared
  `RELIABLE`. The tombstone is sent through it and continues the field puts' sequence.
  (Field puts go through `session.put()` under the publisher's ID, so the reliability
  setting only affects the tombstone.)
- Schema generation no longer crashes on annotations it can't resolve (e.g. forward
  references to function-local classes). They're described as "any value".

### Improvements

- **Protocol spec rewritten and checked against the wire.** `docs/protocol.md` now documents
  the three built-in callables and their mount points, the full schema format, wire
  annotations and `x-rpc-endpoints`, plus tombstones, codec-encoded fields, per-publisher
  `SourceInfo`, implementer roles and a checklist. Corrections: RPC parameters are
  `;`-separated (not `&`), `sr_metadata` is also served at the bare key, echo suppression
  compares zid **and** eid, and list-all discovery is `**/sr_metadata`.

### Tests

- `tests/test_protocol.py` checks the spec's wire-level claims with raw zenoh calls. Strict
  `xfail` tests pin the two known bugs (`;` in RPC arguments, Enum fields).
- `tests/test_schema.py` checks every generated schema against the JSON Schema 2020-12
  meta-schema and validates real `sr_rehydrate` payloads against their schema. Adds
  `jsonschema` as a dev dependency.
- CLI tests for `topic schema`, `topic rehydrate`, `topic rpc` and `meta type_schema` now
  assert on the printed output. They previously checked only the exit code, which is 0
  even when nothing answers, and that's how the missing `sr_type_schema` went unnoticed.

## v0.2.0

### Features

- **Relative topics: `./camera` means `<base_topic>/camera`.** A leading `.` chunk expands to
  the session's base topic (see below for how it's chosen), so `./camera` and
  `myhost/camera` name the same object. Accepted by `SyncableObject` constructors,
  `from_topic`, `from_topic_untyped`, `list_topics(prefix=...)`, and every CLI topic argument
  and `--prefix` (`.` needs no shell quoting, unlike `~`). Resolution happens locally; nothing
  changes on the wire. New `Session.resolve_topic()` exposes the expansion.

- **Per-field QoS.** Declare
  `<field>_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.DATA_HIGH)` on a
  `SyncableObject` to set Zenoh priority, congestion control and express for that field's
  publishes. Assigning on an instance overrides the class default for later publishes without emitting
  an event, at any nesting depth. A nested field's `_qos` goes on the sub-object class that
  declares it, and nested paths without one fall back to the nearest ancestor's `_qos`. Container publishes
  use their top-level field's. QoS is sender-local: never synced, rehydrated or in the schema.

- **Custom publish and receive per field.** `<field>_publish(self, value)` replaces the
  default publish. It yields any number of `PutArgs` dicts: keyword arguments passed
  straight to `zenoh.Session.put`, typed as a `TypedDict` so type checkers catch bad keys.
  Bare `bytes`/`str` also work. Each put carries the publisher's `source_info` so echo
  suppression works, and QoS keys left out come from the field's `_qos`.
  `<field>_receive(self, value, sample)` runs after codec decoding and returns the value to
  apply or `SKIP`. The type check runs on that returned value. Hooks for a nested field go on
  the sub-object that declares it (`Leaf.x_publish` handles `mid/leaf/x`, bound to that
  `Leaf`), with no ancestor fallback. This enables formats like
  progressive MJPEG, where one assignment becomes several chunks at different priorities.

- **Base topic can come from `/etc/spirisynq_base_topic`.** The default base topic is now,
  in order: `SPIRI_SYNQ_BASE_TOPIC`, the contents of `/etc/spirisynq_base_topic`, then
  `socket.gethostname()`. Inside a container the kernel hostname is per-container, so
  objects published from different containers on the same machine ended up under different
  prefixes. Bind-mounting the host's `/etc/hostname` to `/etc/spirisynq_base_topic` gives
  every container the machine's name without overriding the container's own `/etc/hostname`.
  Hosts without the file behave as before.

### Improvements

- **Topics are validated up front.** Object topics and CLI topic arguments are checked with
  `zenoh.KeyExpr` when resolved, raising `ValueError` (or a clean CLI error) instead of
  failing later inside zenoh. `.` anywhere but the first chunk, and `..` anywhere, are
  rejected, since zenoh would otherwise accept them as literal keys.

### Fixes

- **`source_sn` is counted per object topic, not per field name.** `Session.source_info` keyed
  its sequence counter by the relative field path, so every object on a session sharing a
  field name (e.g. `image`) advanced the same counter, while different fields of one object
  had unrelated counters. Sequence numbers are now monotonic per source entity (zid + eid),
  i.e. one counter per object's publisher, matching zenoh's own `source_sn` semantics, so a
  subscriber on `<topic>/**` can use them for gap and reorder detection. `Session.source_info()` no longer takes a `path` argument.

- **The CLI no longer opens two zenoh sessions.** `cli.py` constructed its own `Session()`
  while importing `SpiriSynq.session` already creates the module-level default session, so
  every `synq` invocation started (and joined the network with) an extra, unused session.
  The CLI now uses the default session via `current_session.get()`.

- **RPC replies are keyed by the queryable, not the query.** `@remote_method` handlers (and
  the built-in `sr_*` queryables) replied with `query.key_expr`, so a wildcard query such as
  `**/sr_object_schema` got every reply keyed by the wildcard itself. `synq topic rpc` with
  `--prefix` or no topic therefore reported each topic as `**`. Replies now use the
  queryable's own concrete key. Custom `@method.server()` handlers reply themselves and
  should do the same; the example in `docs/concepts.md` has been updated.

### Tests

- **Fixed racy publish-from-another-session tests.** Several `test_syncable_objects.py` tests
  waited on `obj.synq_publisher.matching_status`, which `obj`'s own subscriber satisfies
  immediately, then sent a single put from a third session that zenoh could drop before a
  route to the mirror existed (~10% failure rate at v0.1.4). They now re-send the
  idempotent put until the mirror reacts.

- `tests/test_qos_hooks.py` covers QoS resolution and its on-wire priority, the hook round
  trip, `SKIP`, type-checking of hook results, and hook-less peers.

## v0.1.4

### Fixes

- **`synq topic list` now separates entries.** Each reply was printed back-to-back with no
  delimiter, making results run together. Every entry now starts with a `---` YAML document
  marker, so the output is readable and parses as a multi-document YAML stream.

- **`synq topic watch --no-truncate` no longer truncates long values.** The watch output
  passed an explicit `overflow="ellipsis"` to rich, which overrides the console's soft-wrap
  setting, so long payloads (e.g. base64 `!!binary` images) were cut off with `…` even with
  `--no-truncate` or when piped. The ellipsis is now only applied when truncation is enabled.

## v0.1.3

### Features

- **`Session.from_topic_untyped(topic)` mirrors objects without a locally importable dataclass.**
  Discovers field names and wire type tags via `sr_object_schema`/`sr_metadata`, synthesizes a
  matching `SyncableObject` subclass with every field typed `object`, and returns a mirror --
  useful for generic tooling and peers written in another language whose class definitions
  aren't available in this process.

### Fixes

- **RPC schema generation no longer crashes on modules using `from __future__ import annotations`.**
  `get_schema()` used `inspect.signature()` annotations directly, which are raw strings under
  PEP 563 deferred evaluation, breaking `resolve_type()` for any `@remote_method` parameter or
  return type in such a module (e.g. `SpiriCamera.Camera`). Parameter and return type hints are
  now resolved via `typing.get_type_hints()` instead.

- **`SyncableObject`'s self-echo filter now keys on the specific publisher, not the whole
  zenoh session.** `_zenoh_receive_changes` used to skip a sample if its `zid` matched the
  local session's `zid` -- but a `zid` identifies an entire zenoh `Session`, shared by every
  `SyncableObject` constructed on it. Two objects on the same session (e.g. an authoritative
  object and a separate mirror subscribed to its topic in the same process) collided: the
  mirror's subscriber discarded the authoritative object's genuine publishes as if they were
  its own echo, so the mirror silently stopped updating after its initial rehydrate. The
  filter now compares each sample's source against the ids of this object's own declared
  publishers (snapshotted per-instance at `sync()` time), so only an object's actual own
  publish is filtered.

- **`click` is now a direct dependency again.** It was dropped after removing the
  `help-all` command (the only place we imported it directly), on the assumption
  that `typer` always pulls it in transitively. That assumption doesn't hold for
  every `typer` release/resolution, and `uv run spirisynq` / `uvx spirisynq` broke
  without it. Declaring `click>=8.3.3` directly keeps the CLI working regardless
  of what `typer` happens to declare.

### Improvements

- **`RpcException` now includes the topic in the error message.**
  All raise sites pass the zenoh selector as a `topic` keyword argument, which is
  prepended to the message as `[<topic>]` and also accessible as `exc.topic`.

- **`synq topic call` now includes the topic in RPC error output.**
  The CLI error line reads `RPC error on <selector>: <message>` instead of omitting
  the selector, making it easier to identify which endpoint failed.

- **Server-side RPC errors now log a full traceback.**
  Both error handlers in `_zenoh_callback` use `logger.exception(...)` instead of
  `logger.error(...)`, so the full stack trace is captured in server logs alongside
  the topic that triggered the error.

## v0.1.2

### Bug Fixes

- **Fixed `synq_lazy_publish` never actually suppressing publishes when there are no subscribers.**
  The check used `not self.synq_publisher.matching_status`, but `matching_status` returns a
  `MatchingStatus` object (always truthy), so the guard was dead code. Fixed to use
  `.matching_status.matching` to read the actual boolean.

### Tests

- **Replaced timing sleeps before zenoh publishes with `matching_status.matching` polls.**
  Several tests used `time.sleep()` to wait for subscription routing to propagate before
  publishing a message. The fix polls `obj.synq_publisher.matching_status.matching` instead,
  which resolves as soon as the router has registered the subscriber — no fixed wait needed.

### Internal

- **Replaced `ruamel-yaml` with `PyYAML` for thread-safe serialization.**
  `ruamel.yaml` holds shared mutable state on the `YAML` instance, requiring
  a lock around every serialize/deserialize call and a non-trivial `IsolatedYAML`
  subclass to work around emitter state poisoning and class-level dict aliasing.
  PyYAML creates a fresh `Loader`/`Dumper` instance per call, so there is no
  shared state between threads at all — no locks needed. The `IsolatedYAML`
  workaround is removed; a new `SessionSerializer` class in `SpiriSynq/serializer.py`
  holds per-session `SafeLoader`/`SafeDumper` subclasses with registered types in
  their class-level dicts. PyYAML 6.0.3+ also ships free-threaded (`cp314t`) wheels,
  making this the correct path for GIL-free Python.

### Features

- **Authoritative `SyncableObject` now sends a reliable zenoh tombstone on `close()`.**
  When the authoritative node closes an object, a `SampleKind.DELETE` is published on
  `synq_absolute_path/**` with `Reliability.RELIABLE`. Mirrors receive this and set
  `synq_is_deleted = True` and emit `synq_signal_tombstone`. The sender's own subscriber
  ignores the echo via ZID filtering.

- **Added `spirisynq` as a CLI entry point alias.**
  The CLI is now also registered as `spirisynq`, enabling `uvx spirisynq` for
  zero-install usage. The existing `synq` command is unchanged.

- **`SyncableObject` now has a minimal `__str__` representation.**
  Shows the class name, `synq_absolute_path`, and any user-declared fields
  (fields not prefixed with `synq_` or `_`). Avoids printing session internals.

## v0.1.1

### Features

- **New `topic bandwidth` CLI command for monitoring message throughput.**
  Subscribe to any key expression (wildcards supported) and get a live
  bytes/sec and msg/sec readout. `--bytes` outputs raw byte counts per
  interval to stdout for machine-readable consumption.

- **`SyncableObject` now tracks `synq_mtime`: the monotonic timestamp of the last received remote update.**
  Set to `time.monotonic()` whenever a remote update is successfully applied;
  `-1` before any remote update arrives. Useful for detecting stale objects or
  diagnosing receive latency. Never published or synced — monotonic clocks are
  process-local and meaningless across nodes.

### Tests

- **Refactored test suite to use TCP-only zenoh sessions instead of UDP multicast.**
  A shared seed session now listens on a random TCP port, and all test sessions
  connect to it via `zenoh_test_config()` with multicast scouting disabled. This
  eliminates cross-test interference from UDP broadcast and makes tests faster and
  more deterministic. A `_send_and_wait` retry helper was added to handle the
  remaining cases where zenoh may silently drop a message before a subscriber is
  ready. CLI tests now share the CLI's own session (`synq_session=cli_session`)
  rather than creating independent sessions, fixing intermittent isolation failures.

### Bug Fixes

- **`RootFrame` is now correctly serialized by the YAML type registry.**
  `RootFrame` is a `str` subclass, and `register_type_recursive` was skipping
  its registration because the MRO check found `str` already representable.
  But ruamel.yaml's representer only checks the exact type at serialize time,
  so `RootFrame` values raised `RepresenterError` at runtime. Fixed by adding
  `yaml_tag`, `to_yaml`, and `from_yaml` to `RootFrame`, and by tightening
  `_is_representable` to check the exact type only — matching what ruamel.yaml
  actually does. `RootFrame` round-trips correctly through YAML so
  `isinstance(frame, RootFrame)` is preserved after deserialization.

- **CLI `topic call` now exits with code 2 when no reply is received.**
  Previously, calling a non-existent or unreachable RPC endpoint would silently
  succeed (exit 0). Now the CLI detects the empty-reply case and exits with
  code 2, making it distinguishable from an RPC error (exit 1).

- **Type checking now applies to codec-decoded values, not just YAML payloads.**
  Binary payloads using `ZENOH_BYTES` encoding were bypassing the type-mismatch
  check because the codec guard short-circuited validation. Type mismatches on
  codec-decoded fields now correctly fire the mismatch signal.

- **`topic list` now returns all matching objects instead of just one.**
  Zenoh's default consolidation mode deduplicates replies that share the same
  reply key, so when multiple authoritative objects of the same class responded
  to a wildcard `get()`, only one result survived. The fix passes
  `ConsolidationMode.NONE` to the `get()` call so every reply is kept.

## v0.1.0
