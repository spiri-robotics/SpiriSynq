# Overview

SpiriSynq keeps Python dataclass instances in sync across processes, machines, and languages over a [Zenoh](https://zenoh.io) pub/sub network.

The core idea: you define a dataclass, mark it as a `SyncableObject`, and any number of instances across the network pointing at the same topic stay in sync — field changes published by any one of them are received by all the others.

```python
from dataclasses import dataclass
from SpiriSynq.syncable_objects import SyncableObject

@dataclass
class Telemetry(SyncableObject):
    altitude: float = 0.0
    battery: int = 100

# Process A — owns the data
t = Telemetry("drone/telemetry", synq_authoritive=True)
t.altitude = 42.5  # automatically published to the network

# Process B — mirrors it
mirror = Telemetry.from_topic("drone/telemetry")
print(mirror.altitude)  # 42.5
```

## What SpiriSynq is not

- Not a database or message queue — there is no persistence, history, or delivery guarantee beyond what Zenoh provides.
- Not an RPC framework — though it includes `@remote_method` for calling methods on authoritative objects, that is a secondary feature built on the same Zenoh substrate.
- Not limited to Python — any node that speaks the [SpiriSynq protocol](protocol.md) can participate. See [Protocol Specification](protocol.md) for the wire format.

## When to use it

SpiriSynq is a good fit when:

- You have structured, typed state that multiple processes need to observe or act on.
- You want discovery ("what objects are on the network right now?") without a central broker.
- You need to cross language or OS boundaries and want a well-defined contract between sides.
