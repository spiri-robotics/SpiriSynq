# SpiriSynq

SpiriSynq keeps Python dataclass instances in sync across processes, machines, and languages over a [Zenoh](https://zenoh.io) pub/sub network. Define a typed dataclass, point multiple instances at the same topic, and field changes flow between them automatically.

```python
from dataclasses import dataclass
from SpiriSynq.syncable_objects import SyncableObject

@dataclass
class Telemetry(SyncableObject):
    altitude: float = 0.0
    battery: int = 100

# Process A — owns the data
t = Telemetry("drone/telemetry", synq_authoritive=True)
t.altitude = 42.5  # published to the network automatically

# Process B — mirrors it
mirror = Telemetry.from_topic("drone/telemetry")
print(mirror.altitude)  # 42.5
```

## Installation

```bash
pip install SpiriSynq
```

Requires Python 3.13+. A Zenoh router is optional for local use — peers discover each other directly.

## How it works

Each syncable field maps to a Zenoh key expression `<topic>/<field>`. When a field changes, psygnal fires a signal, SpiriSynq serialises the new value to YAML, and publishes it. All subscribers on the same topic receive the update and apply it in-place without echo-looping it back.

Nested dataclass fields sync at the sub-field level: a change to `robot.gps.latitude` publishes to `<topic>/gps/latitude`, not `<topic>/gps`. Inherit from `SubSyncableDataclass` to make a nested type fully evented without Zenoh overhead; alternatively, use `@dataclass(frozen=True)` for immutable value objects that are replaced atomically.

## Authoritative vs mirror

Sync is **bidirectional** — every instance both publishes its own changes and receives changes from others. `synq_authoritive` controls only the queryable side:

- **Authoritative** (`synq_authoritive=True`): registers Zenoh queryables for full-state rehydration on connect, `@remote_method` RPCs, and schema/discovery endpoints. Sets the base topic prefix from the hostname.
- **Mirror** (default): publishes and receives field changes just like an authoritative instance, but forwards RPC calls to the authoritative node rather than running them locally.

```python
# Both sides can write — sync is bidirectional
counter = Counter("myapp/counter", synq_authoritive=True)
mirror = Counter.from_topic("myapp/counter")
mirror.value = 99  # received by counter
```

## Remote methods

`@remote_method` exposes a method as a Zenoh queryable. Mirrors call it transparently — the call is routed to the authoritative node and the return value is sent back.

```python
from SpiriSynq.remote_callables import remote_method

@dataclass
class Robot(SyncableObject):
    status: str = "idle"

    @remote_method
    def arm(self, mode: str = "auto") -> str:
        self.status = "armed"
        return f"armed in {mode} mode"

# From a mirror — works exactly like a local call
robot = Robot.from_topic("fleet/robot1")
result = robot.arm(mode="manual")  # "armed in manual mode"
```

Generator and async generator methods are supported. The mirror receives a regular Python generator that streams values as each reply arrives.

Custom timeouts: `robot.arm.timeout(5.0)(mode="manual")`.

## Discovery

```python
from SpiriSynq.session import current_session

session = current_session.get()
for metadata in session.list_topics():
    print(metadata["topic"], metadata["classes"])

# Filter by type
for metadata in session.list_topics(type_filter="Robot"):
    robot = Robot.from_topic(metadata["topic"])
```

## Cross-language compatibility

The wire format is plain Zenoh with YAML payloads. Any node that implements the [SpiriSynq protocol](docs/protocol.md) — four mandatory queryables and per-field puts — is a first-class participant. No library required on the other end.

## Key configuration options

| Field | Default | Purpose |
|---|---|---|
| `synq_authoritive` | `False` | Register queryables; answer rehydration and RPC calls |
| `synq_publish` | `True` | Publish local changes to the network |
| `synq_receive` | `True` | Apply incoming changes from the network |
| `sync_lazy_publish` | `False` | Skip publishing when no subscribers are present |
| `synq_auto_start` | `True` | Call `sync()` automatically on construction |

## Docs

- [Overview](docs/overview.md)
- [Getting Started](docs/getting_started.md)
- [Concepts](docs/concepts.md)
- [Protocol Specification](docs/protocol.md)
