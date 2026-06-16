# Getting Started

## Installation

```bash
pip install SpiriSynq
```

SpiriSynq requires Python 3.13+. A Zenoh router is optional for local use — peers can discover each other directly.

## Your first syncable object

Define a dataclass that inherits from `SyncableObject`. Every field becomes a syncable field — changes are published automatically.

Save this as `counter.py` and run it:

```python
import time
from dataclasses import dataclass
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.remote_callables import remote_method

@dataclass
class Counter(SyncableObject):
    value: int = 0

    @remote_method()
    def reset(self, to: int = 0) -> int:
        self.value = to
        return self.value

counter = Counter("myapp/counter", synq_authoritive=True)
print(f"Publishing on: {counter.synq_absolute_path}")

while True:
    counter.value += 1
    time.sleep(1)
```

```
$ python counter.py
Publishing on: myhost/myapp/counter
```

While it runs, open a second terminal and watch the field changes arrive in real time:

```
$ python -m SpiriSynq.cli topic watch myhost/myapp/counter
received: 2026-01-01T00:00:01.000000+00:00
value: 0
---
received: 2026-01-01T00:00:02.000000+00:00
value: 1
---
received: 2026-01-01T00:00:03.000000+00:00
value: 2
---
```

Or fetch the current state at any point without waiting for an update:

```
$ python -m SpiriSynq.cli topic rehydrate myhost/myapp/counter
!Counter
value: 7
```

To call the `reset` RPC from another process, create a mirror and call the method directly. Save this as `reset_counter.py`:

```python
from dataclasses import dataclass
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.remote_callables import remote_method

@dataclass
class Counter(SyncableObject):
    value: int = 0

    @remote_method()
    def reset(self, to: int = 0) -> int:
        self.value = to
        return self.value

mirror = Counter.from_topic("myhost/myapp/counter")
result = mirror.reset(to=0)
print(f"Counter reset, value is now {result}")
```

```
$ python reset_counter.py
Counter reset, value is now 0
```

The call is routed to the authoritative process over Zenoh and the return value is sent back. The `counter.py` script will also receive the field update and continue incrementing from 0.

You can also call RPCs directly from the CLI without writing a script:

```
$ python -m SpiriSynq.cli topic call myhost/myapp/counter/reset to=0
0
```

Arguments are passed as `key=value` pairs where values are YAML literals (`to=0` sends an integer, `name=world` sends a string, `flag=true` sends a boolean).

To see all objects currently on the network:

```
$ python -m SpiriSynq.cli topic list
topic: myhost/myapp/counter
classes:
  - '!Counter'
  - '!SyncableObject'
authoritive_node: abc123...
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

The `Counter` above already has a `@remote_method` — `reset` is callable from any mirror instance and is transparently routed to the authoritative node over Zenoh:

```python
mirror = Counter.from_topic("myhost/myapp/counter")
result = mirror.reset(to=0)
print(result)  # 0
```

Generator methods are also supported — the caller receives a regular Python generator that streams values from the authoritative node:

```python
@remote_method()
def count_down(self, from_value: int = 10):
    for i in range(from_value, -1, -1):
        self.value = i
        yield i

for value in mirror.count_down(from_value=5):
    print(value)
```

Generator methods also work from the CLI — each yielded value is printed separated by `---`:

```
$ python -m SpiriSynq.cli topic call myhost/myapp/counter/count_down from_value=3
3
---
2
---
1
---
0
```

Use `@method.client()` to post-process the return value on the mirror side only — useful for decoding, unit conversion, or any transform you don't want to run on the server:

```python
@remote_method()
def get_reading(self) -> bytes:
    return self._sensor.read_raw()

@get_reading.client()
def get_reading(self, result: bytes) -> float:
    return struct.unpack("f", result)[0]

value = mirror.get_reading()  # float, decoded on the caller
```

See [Concepts — Client-side transforms](concepts.md#client-side-transforms) for full details including generator and async support.

## Cleanup

Objects undeclare their Zenoh resources when garbage collected. For deterministic cleanup, call `obj.close()` explicitly.

## Integrating with existing code

SpiriSynq does not own the main loop. Zenoh I/O runs on its own background threads, so you can drop a `SyncableObject` into any existing application — an asyncio service, a robotics framework, a game loop — without yielding control.

For a long-running producer, spin up your own thread and write to the object's fields from it.

```python
import threading
from dataclasses import dataclass
from SpiriSynq.syncable_objects import SyncableObject

@dataclass
class Sensor(SyncableObject):
    reading: float = 0.0

sensor = Sensor("myapp/sensor", synq_authoritive=True)

def read_loop():
    while True:
        sensor.reading = hardware.read()

threading.Thread(target=read_loop, daemon=True).start()

# your existing main loop continues here uninterrupted
```

For asyncio, use `.as_async()` on remote method calls so you don't block the event loop:

```python
mirror = Sensor.from_topic("myhost/myapp/sensor")

async def main():
    result = await mirror.calibrate.as_async()
```

