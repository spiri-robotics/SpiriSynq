# Changelog

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
