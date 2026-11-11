# Reacting to changes

There are two ways to run code when a field changes:

- **Callbacks declared on the class**, with `@on_change.owner` or `@on_change.mirror`. They are
  part of the type.
- **Signals on `events`**, which any Python code can connect to at run time. They are for your own
  program, not part of the type.

## Callbacks on the owner

`@on_change.owner("field")` runs a method on the owner every time that field changes, whatever
changed it: the owner's own code, a mirror's write, or a remote method. It is given the new value.

The robot's driver sets `current_a` many times a second. The team wants the highest current seen,
published as `peak_current_a`, so they can size the wiring. That is a rule about the battery,
whatever code sets `current_a`:

```python
from SpiriSynq import on_change


@dataclass
class Battery(SyncableObject):
    current_a: float = 0.0
    peak_current_a: float = 0.0

    @on_change.owner("current_a")
    def track_peak(self, value: float) -> None:
        if value > self.peak_current_a:
            self.peak_current_a = value
```

```python
battery.current_a = 31.0
battery.peak_current_a   # 31.0
battery.current_a = 12.0
battery.peak_current_a   # still 31.0
```

Fields an owner callback sets are published like any other change, so every mirror sees the new
peak.

Use an owner callback for a rule that must hold whenever a field changes, however it changes:
derived fields, clamping a value a mirror wrote, resetting state.

## Callbacks on mirrors

`@on_change.mirror("field")` runs a method on every mirror, each time a new value of that field
arrives.

`current_a` jumps around too much to read on the dashboard, so the dashboard wants a smoothed
value. The robot shouldn't spend its time or its network on something only displays need:

```python
@dataclass
class Battery(SyncableObject):
    current_a: float = 0.0
    smoothed_current_a: float = 0.0

    @on_change.mirror("current_a")
    def smooth_current(self, value: float) -> None:
        self.smoothed_current_a = 0.9 * self.smoothed_current_a + 0.1 * value
```

Fields a mirror callback sets change **only that mirror's copy**. They aren't published, so a
hundred dashboards don't each write their smoothed value back to the robot. If the owner later
publishes a value for the field, the owner's value replaces it.

Use a mirror callback for work every reader of the object needs, but the owner shouldn't do or
publish.

## Both kinds

- A callback can watch several fields: `@on_change.owner("charge_mah", "capacity_mah")`. It is then
  also given the name of the field that changed, as `field`.
- Setting the watched field inside its own callback doesn't run the callback again.

## Signals on `events`

Every field has a signal on `obj.events`, on the owner and on every mirror. It fires on every
change to that copy, whether the change was local or arrived from the network, including changes
made by callbacks:

```python
mirror = Battery.from_topic("robot1/battery")
mirror.events.smoothed_current_a.connect(update_gauge)
```

Connect to `obj.events` itself to hear about every field. Disconnect with the same callable.

Use signals for what your own program does with a change: drawing it, logging it, or, on the
owner, driving hardware:

```python
battery = Battery("./battery", synq_authoritive=True)
battery.events.charge_limit_percent.connect(charger.set_limit)
```

### Threads and asyncio

Signal handlers and callbacks run on SpiriSynq's network thread. Keep them short. To hand values
to asyncio, use `loop.call_soon_threadsafe`:

```python
import asyncio


async def main():
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[float] = asyncio.Queue()
    mirror.events.current_a.connect(
        lambda value: loop.call_soon_threadsafe(queue.put_nowait, value)
    )
    while True:
        print(await queue.get())
```
