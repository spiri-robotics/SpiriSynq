# Changelog

## Unreleased

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
