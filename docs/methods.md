# Methods and properties

Ordinary methods and properties on a syncable object run **wherever you use them**: on the owner's
copy when the owner calls them, on a mirror's copy when a mirror does. They work on the fields of
that copy, and nothing goes over the network.

The examples on this and the following pages use one class: the battery of a robot. A process
on the robot owns the `Battery`. A ground station and a web dashboard mirror it.

```python
from dataclasses import dataclass

from SpiriSynq.syncable_objects import SyncableObject


@dataclass
class Battery(SyncableObject):
    charge_mah: float = 0.0
    capacity_mah: float = 5000.0
    current_a: float = 0.0

    @property
    def percent(self) -> float:
        return 100 * self.charge_mah / self.capacity_mah

    def minutes_left(self, draw_a: float) -> float:
        return (self.charge_mah / 1000) / draw_a * 60
```

## Properties

A property is a value worked out from the fields, and read like a field:

```python
mirror = Battery.from_topic("robot1/battery")
mirror.percent   # 62.5
```

Properties keep old readers working when a type changes. The battery used to publish a
`percent` field. It now publishes `charge_mah` and `capacity_mah`, which say more. Dashboards
written before the change still read `percent`, and the property gives it to them.

Use a property for a value fully determined by the fields: one you'd otherwise publish as a
redundant field, or one a removed field's readers still need.

## Methods

A method can take arguments, unlike a property. The ground station wants to know how long the
battery lasts at the draw its planned mission needs:

```python
mirror.minutes_left(draw_a=12.0)   # 15.6
```

The answer depends on an argument the owner doesn't know, and asking the owner would be a round
trip for arithmetic the ground station can do itself.

A method can also set fields. On a mirror, that is a write like any other: it is published, and
the owner applies it (see [Writes from mirrors](objects.md#writes-from-mirrors)).

Use a method for a calculation over the fields that readers need. For work that must happen on the owner, use a
[remote method](remote_methods.md).
