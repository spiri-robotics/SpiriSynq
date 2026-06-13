# Getting Started

## Installation

```bash
pip install SpiriSynq
```

SpiriSynq requires Python 3.13+. A Zenoh router is optional for local use — peers can discover each other directly.

## Your first syncable object

Define a dataclass that inherits from `SyncableObject`. Every field you declare becomes a syncable field — changes are published to the network automatically.

```python
from dataclasses import dataclass
from SpiriSynq.syncable_objects import SyncableObject

@dataclass
class Counter(SyncableObject):
    value: int = 0
```

## Authoritative vs mirror

Sync is **bidirectional** — any instance can publish field changes and all others on the same topic receive them. The `synq_authoritive` flag controls something narrower: which instance answers queryable calls like `sr_rehydrate` (full-state fetch on connect) and `@remote_method` RPCs.

- `synq_authoritive=True` — responds to rehydration requests and RPC calls. Also sets the base topic prefix from the hostname automatically.
- Mirror (default, `synq_authoritive=False`) — still publishes and receives field changes, but does not register queryables.

```python
# Process A
counter = Counter("myapp/counter", synq_authoritive=True)
counter.value = 1

# Process B — can also write
mirror = Counter.from_topic("myapp/counter")
mirror.value = 2  # published back; process A receives it
```

In practice you'll usually have one authoritative instance that "owns" the initial state and handles RPCs, but the field sync itself flows both ways.

## Topics

The first argument to a `SyncableObject` is the **topic** — a slash-separated Zenoh key expression that identifies this object on the network. By default, authoritative objects are published under `<hostname>/<topic>`. You can override this with `synq_base_topic` or the `SPIRI_SYNQ_BASE_TOPIC` environment variable.

```python
counter = Counter("counter", synq_authoritive=True)
print(counter.synq_absolute_path)  # e.g. "myhost/counter"
```

## Discovering objects

Use `session.list_topics()` to find all objects currently on the network:

```python
from SpiriSynq.session import current_session

session = current_session.get()
for metadata in session.list_topics():
    print(metadata["topic"], metadata["classes"])
```

You can filter by type tag:

```python
for metadata in session.list_topics(type_filter="Counter"):
    ...
```

## Remote methods

Decorate a method with `@remote_method` to expose it as a callable from mirror instances. Calls are transparently routed to the authoritative node over Zenoh.

```python
from SpiriSynq.remote_callables import remote_method

@dataclass
class Robot(SyncableObject):
    status: str = "idle"

    @remote_method
    def arm(self, mode: str = "auto") -> str:
        self.status = "armed"
        return f"armed in {mode} mode"
```

```python
# From a mirror instance
robot = Robot.from_topic("fleet/robot1")
result = robot.arm(mode="manual")
print(result)  # "armed in manual mode"
```

Generator methods are also supported — the caller receives a regular Python generator that streams values from the authoritative node:

```python
@remote_method
def scan(self):
    for i in range(10):
        yield i
```

## Cleanup

Objects undeclare their Zenoh resources when garbage collected. For deterministic cleanup, call `obj.close()` explicitly.
