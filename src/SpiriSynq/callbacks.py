"""`@on_change.owner` and `@on_change.mirror`: methods that run when a field changes.

```python
@on_change.owner("current_a")
def track_peak(self, value: float) -> None: ...
```

A callback watching several fields is also given the one that changed, as `field`.
"""

from __future__ import annotations

from collections.abc import Callable


class OnChange:
    """A method run on the owner, or on each mirror, when one of its fields changes."""

    def __init__(self, side: str, fields: tuple[str, ...], func: Callable):
        if not fields:
            raise TypeError(f"@on_change.{side} needs at least one field")
        self.side = side
        self.fields = fields
        self.func = func
        self.name = getattr(func, "__name__", "callback")

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return self.func.__get__(obj, objtype)

    def run(self, obj, field: str, value) -> None:
        if len(self.fields) > 1:
            self.func(obj, value, field=field)
        else:
            self.func(obj, value)


class on_change:  # noqa: N801 - used as a decorator namespace: @on_change.owner(...)
    """Decorators for field callbacks: `@on_change.owner(*fields)`, `@on_change.mirror(*fields)`."""

    @staticmethod
    def owner(*fields: str) -> Callable[[Callable], OnChange]:
        """Run on the owner each time one of `fields` changes, however it changed. Fields it
        sets are published."""
        return lambda func: OnChange("owner", fields, func)

    @staticmethod
    def mirror(*fields: str) -> Callable[[Callable], OnChange]:
        """Run on each mirror each time a new value of one of `fields` arrives. Fields it sets
        change only that mirror's copy."""
        return lambda func: OnChange("mirror", fields, func)
