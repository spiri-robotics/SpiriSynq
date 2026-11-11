# Getting started

## Installation

```bash
pip install SpiriSynq
```

SpiriSynq requires Python 3.13 or newer. The package ships the Rust core as a compiled
extension, in wheels for the common platforms.

## Your first object

Define a dataclass that inherits from `SyncableObject`. Its fields are synced; methods marked
`@remote_method` can be called from other processes.

Both processes in this example need the class, so put it in its own module, `counter_types.py`:

```python
from dataclasses import dataclass

from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.remote_callables import remote_method


@dataclass
class Counter(SyncableObject):
    synq_type_id = "0b7e3c52-9a41-4f6d-8e2b-5c19d7a4f803:1"
    value: int = 0

    @remote_method
    def reset(self, to: int = 0) -> int:
        self.value = to
        return self.value
```

`synq_type_id` names the type on the network. A class in a package published on PyPI gets one
automatically; a class in a script or an unpublished module needs one written in. See
[Type identity and schemas](type_identity.md).

The owner, `counter.py`:

```python
import time

from counter_types import Counter

counter = Counter("./counter", synq_authoritive=True)
print(f"Publishing on: {counter.synq_absolute_path}")

while True:
    counter.value += 1
    time.sleep(1)
```

```
$ python counter.py
Publishing on: myhost/counter
```

`./counter` is relative to this machine's base topic, which is its hostname by default. See
[Topics](objects.md#topics).

## Mirroring it from another process

In a second process, mirror the same topic. The mirror fetches the current state from the owner,
then follows every change:

```python
from counter_types import Counter

mirror = Counter.from_topic("myhost/counter")
print(mirror.value)   # the owner's current value

mirror.reset(to=0)    # runs on the owner; returns 0
```

`mirror.reset(...)` doesn't run the method body locally. It sends the call to the owner and
returns the owner's result. The owner's `value` changes to 0, and the mirror receives that
change like any other.

## Reacting to changes

Rather than polling a mirror, connect a callback to a field's signal:

```python
def on_value(value: int) -> None:
    print(f"value is now {value}")

mirror.events.value.connect(on_value)
```

Callbacks run on SpiriSynq's network thread, not your main thread. Keep them short, and hand
heavy work to your own thread or event loop. See
[Reacting to changes](callbacks.md).

## Knowing whether the owner is there

A mirror can tell whether the object's owner is alive:

```python
print(mirror.synq_alive)
```

See [Liveliness](liveliness.md).

## Cleaning up

An object releases its network resources when it is garbage collected. Call `obj.close()` to
release them at a known time. Closing an authoritative object withdraws its liveliness, so its
mirrors see it go away.
