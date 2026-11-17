"""Presets for field settings: a field's priority (``<field>_qos``) and reliability
(``<field>_reliability``).

```python
@dataclass
class Camera(SyncableObject):
    image: bytes = b""
    image_qos = qos.background
    recording: bool = False
    recording_reliability = qos.reliable
```
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Priority:
    """A field's priority on the network: one of the presets below."""

    name: str

    def __repr__(self) -> str:
        return f"qos.{self.name}"


@dataclass(frozen=True)
class Reliability:
    """Whether a value lost in transit is sent again: `drop` or `reliable`."""

    reliable: bool

    def __repr__(self) -> str:
        return "qos.reliable" if self.reliable else "qos.drop"


realtime = Priority("real_time")
"""Control loops and anything that must not wait."""
interactive_high = Priority("interactive_high")
"""Values a person is waiting on."""
interactive_low = Priority("interactive_low")
data_high = Priority("data_high")
data = Priority("data")
"""The default."""
data_low = Priority("data_low")
background = Priority("background")
"""Bulk data, such as images and logs."""

drop = Reliability(False)
"""Best effort: a value lost in transit stays lost. The default."""
reliable = Reliability(True)
"""A value lost in transit is sent again."""

PRIORITIES = (realtime, interactive_high, interactive_low, data_high, data, data_low, background)
