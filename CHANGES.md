# Changelog

## v0.2.0

### Features

- **Relative topics: `./camera` means `<base_topic>/camera`.** A leading `.` chunk expands to
  the session's base topic (see below for how it's chosen), so `./camera` and
  `myhost/camera` name the same object. Accepted by `SyncableObject` constructors,
  `from_topic`, `from_topic_untyped`, `list_topics(prefix=...)`, and every CLI topic argument
  and `--prefix` (`.` needs no shell quoting, unlike `~`). Resolution happens locally; nothing
  changes on the wire. New `Session.resolve_topic()` exposes the expansion.

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
