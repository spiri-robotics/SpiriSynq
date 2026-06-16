# CLI Reference

SpiriSynq ships a command-line tool for inspecting and interacting with objects on the network without writing any Python.

```bash
python -m SpiriSynq.cli [OPTIONS] COMMAND [ARGS]...
```

`--truncate / --no-truncate` — long lines are truncated by default on a TTY and left unwrapped when piped. Override with `--no-truncate`.

---

## topic

### `topic list`

Discover all objects currently on the network.

```bash
python -m SpiriSynq.cli topic list
python -m SpiriSynq.cli topic list --type Counter
python -m SpiriSynq.cli topic list --prefix myhost
```

Options:
- `-t / --type` — filter by type name (matches the YAML tag without `!`)
- `-p / --prefix` — restrict the search to a key prefix

### `topic watch`

Subscribe to a topic and stream updates to stdout as YAML (default) or NDJSON. Wildcards are supported.

```bash
python -m SpiriSynq.cli topic watch myhost/drone/telemetry
python -m SpiriSynq.cli topic watch 'myhost/**'
python -m SpiriSynq.cli topic watch myhost/drone/telemetry --json
```

Options:
- `--show-paths / --no-show-paths` — include the key path in each record (default: on when wildcards are used)
- `--json / -j` — emit NDJSON instead of YAML
- `--timestamp / -t` — include the Zenoh message timestamp
- `--received-timestamp / -rt` — include the local receive time (default: on)
- `--count / -n` — exit after receiving N messages (default: 0 = unlimited); useful for scripting or waiting for a topic to appear

Records are separated by `---` when metadata fields are present, making the output valid multi-document YAML and compatible with `topic put`.

Binary payloads are base64-encoded and emitted as a YAML `!!binary` scalar.

### `topic put`

Publish a value to a topic. Accepts a direct argument or a piped stream from `topic watch`.

```bash
# Direct publish
python -m SpiriSynq.cli topic put myhost/drone/telemetry/altitude 42.5

# Replay a captured stream
python -m SpiriSynq.cli topic watch myhost/drone/telemetry | \
    python -m SpiriSynq.cli topic put
```

Options:
- `--input-type auto|yaml|raw` — how to interpret stdin. `auto` tries YAML first and falls back to raw string. `raw` publishes the entire stdin as a single opaque value.

When the piped stream contains `path:` fields (from `topic watch --show-paths`), each record is published to its original path. If a topic prefix is also given, stream paths must be subpaths of it — this prevents accidentally replaying a stream to the wrong part of the tree.

### `topic schema`

Fetch and display the field schema for a topic.

```bash
python -m SpiriSynq.cli topic schema myhost/drone/telemetry
```

### `topic rehydrate`

Fetch the full current state of an object. Useful when you suspect local state is stale, or when connecting to a network without a caching router.

```bash
python -m SpiriSynq.cli topic rehydrate myhost/drone/telemetry
```

---

## zenoh

Low-level Zenoh diagnostics.

### `zenoh info`

Show the local session's ZID and any connected routers and peers.

```bash
python -m SpiriSynq.cli zenoh info
```

### `zenoh scout`

Discover Zenoh routers and peers on the network.

```bash
python -m SpiriSynq.cli zenoh scout
python -m SpiriSynq.cli zenoh scout --timeout 3.0 --no-peers
```

Options:
- `--timeout / -t` — scouting duration in seconds (default: 1.0)
- `--routers / --no-routers` — include routers (default: on)
- `--peers / --no-peers` — include peers (default: on)
- `--clients / --no-clients` — include clients (default: off)

---

## meta

### `meta type_schema`

Retrieve the schema for a registered type by name. Without a type argument, returns all registered types on the network.

```bash
python -m SpiriSynq.cli meta type_schema
python -m SpiriSynq.cli meta type_schema Counter
python -m SpiriSynq.cli meta type_schema Counter --prefix myhost
```

Options:
- `-p / --prefix` — restrict the search to a key prefix (default: `**`)

---

## Recipes

**Capture and replay a topic:**

```bash
python -m SpiriSynq.cli topic watch --no-received-timestamp myhost/drone/telemetry > capture.yaml
cat capture.yaml | python -m SpiriSynq.cli topic put myhost/drone/telemetry
```

**Monitor all field changes across a host:**

```bash
python -m SpiriSynq.cli topic watch --json 'myhost/**'
```

**Check what's on the network:**

```bash
python -m SpiriSynq.cli topic list
python -m SpiriSynq.cli zenoh scout
```
