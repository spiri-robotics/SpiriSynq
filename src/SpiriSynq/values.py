"""Python values to and from the core's values, guided by type annotations.

The core's values are `None`, `bool`, `int`, `float`, `str`, `bytes`, lists, dicts and
`_core.Tagged` for local YAML tags. `to_value` turns any value SpiriSynq can send into one;
`from_value` turns one back into what an annotation asks for, or raises `BadValue` saying why.

- A dataclass is a mapping of its fields, tagged with its `yaml_tag` (`!Gps`) or its name.
- A syncable object, a `Link` and an `ObjectNotFound` are a link: `!Link <topic>`.
- A subclass of `str`, `int` or `float` with a `yaml_tag` is that value, tagged.
- An enum is its value; a tuple or a set is a list.
- A class can say how it travels with `synq_to_value(self)` and a classmethod
  `synq_from_value(cls, value)`.
"""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import enum
import types
import typing
from typing import Any, Literal, Union, get_args, get_origin

from psygnal.containers import EventedDict, EventedList, EventedSet

from SpiriSynq import _core
from SpiriSynq.link import Link

LINK = "Link"


class BadValue(ValueError):  # noqa: N818 - a ValueError, named for what it is
    """A value that doesn't fit its annotation."""


def tag_of(cls: type) -> str:
    """The local tag a dataclass or tagged primitive travels with, without its `!`."""
    return cls.__dict__.get("yaml_tag", f"!{cls.__name__}").removeprefix("!")


def _tagged_primitive(cls: type) -> bool:
    return issubclass(cls, (str, int, float)) and "yaml_tag" in cls.__dict__


def _is_syncable(cls) -> bool:
    from SpiriSynq.syncable_objects import SyncableObject

    return isinstance(cls, type) and issubclass(cls, SyncableObject)


def wire_fields(cls: type) -> list[dataclasses.Field]:
    """The fields of a dataclass that travel: not private, not in its `synq_skip_sync`."""
    skip: set = set()
    for c in cls.__mro__:
        skip |= set(c.__dict__.get("synq_skip_sync", ()))
    return [f for f in dataclasses.fields(cls) if not f.name.startswith("_") and f.name not in skip]


def to_value(obj: Any) -> Any:
    """`obj` as a core value. `TypeError` if it has none."""
    if obj is None or type(obj) in (bool, int, float, str, bytes):
        return obj
    if isinstance(obj, _core.Tagged):
        return obj
    if isinstance(obj, Link):
        return _core.Tagged(LINK, obj.topic)
    if _is_syncable(type(obj)):
        return _core.Tagged(LINK, obj.synq_absolute_path)
    if hasattr(type(obj), "synq_to_value"):
        return to_value(obj.synq_to_value())
    if isinstance(obj, enum.Enum):
        return to_value(obj.value)
    if _tagged_primitive(type(obj)):
        base = next(b for b in (str, bool, int, float) if isinstance(obj, b))
        return _core.Tagged(tag_of(type(obj)), base(obj))
    if isinstance(obj, (bool, int, float, str)):
        return next(b for b in (bool, int, float, str) if isinstance(obj, b))(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return bytes(obj)
    if isinstance(obj, (list, tuple)):
        return [to_value(v) for v in obj]
    if isinstance(obj, (set, frozenset, EventedSet)):
        items = [to_value(v) for v in obj]
        with contextlib.suppress(TypeError):
            items.sort()
        return items
    if isinstance(obj, collections.abc.Mapping):
        return {to_value(k): to_value(v) for k, v in obj.items()}
    if isinstance(obj, collections.abc.Sequence):
        return [to_value(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        fields = {f.name: to_value(getattr(obj, f.name)) for f in wire_fields(type(obj))}
        return _core.Tagged(tag_of(type(obj)), fields)
    raise TypeError(f"a {type(obj).__name__} can't be sent: SpiriSynq has no wire form for it")


_registry: dict[str, type] = {}
"""Dataclasses and tagged primitives by tag, for values whose annotation doesn't say."""


def register(cls: type) -> None:
    """Let `from_value` build `cls` from its tag where an annotation doesn't name it."""
    _registry.setdefault(tag_of(cls), cls)


def _describe(hint) -> str:
    return getattr(hint, "__name__", None) or str(hint).replace("typing.", "")


def from_value(value: Any, hint: Any = Any) -> Any:
    """`value` as the annotation `hint` asks for. `BadValue` if it doesn't fit."""
    if hint is Any or hint is object or isinstance(hint, (str, typing.TypeVar)):
        return _untyped(value)
    if hint is None or hint is type(None):
        if value is None:
            return None
        raise BadValue(f"expected None, got {_kind(value)}")
    origin, args = get_origin(hint), get_args(hint)
    if origin is Literal:
        if value in args:
            return value
        raise BadValue(f"expected one of {list(args)}, got {value!r}")
    if origin is Union or origin is types.UnionType:
        return _union(value, args)
    if origin is not None:
        return _generic(value, origin, args)
    if isinstance(hint, type):
        return _class(value, hint)
    return _untyped(value)


def _kind(value) -> str:
    if isinstance(value, _core.Tagged):
        return f"!{value.tag}"
    return type(value).__name__


def _union(value, members):
    if value is None and type(None) in members:
        return None
    if isinstance(value, _core.Tagged):
        # The member the tag names, before trying the others in order.
        tagged = [m for m in members if isinstance(m, type) and _tag_matches(m, value.tag)]
        members = tagged + [m for m in members if m not in tagged]
    reasons = []
    for member in members:
        try:
            return from_value(value, member)
        except BadValue as e:
            reasons.append(str(e))
    raise BadValue(
        f"fits none of {' | '.join(_describe(m) for m in members)}: {'; '.join(reasons)}"
    )


def _tag_matches(cls: type, tag: str) -> bool:
    if tag == LINK:
        return cls is Link or _is_syncable(cls)
    return (dataclasses.is_dataclass(cls) or _tagged_primitive(cls)) and tag_of(cls) == tag


def _untyped(value):
    """A value no annotation describes: tags it knows become their classes."""
    if isinstance(value, _core.Tagged):
        if value.tag == LINK and isinstance(value.value, str):
            return Link(value.value)
        cls = _registry.get(value.tag)
        if cls is not None:
            try:
                return _class(value, cls)
            except BadValue:
                pass
        return value
    if isinstance(value, list):
        return [_untyped(v) for v in value]
    if isinstance(value, dict):
        return {_untyped(k): _untyped(v) for k, v in value.items()}
    return value


def _seq(value, what: str) -> list:
    if not isinstance(value, list):
        raise BadValue(f"expected {what}, got {_kind(value)}")
    return value


def _generic(value, origin, args):
    item = args[0] if args else Any
    if origin in (list, collections.abc.Sequence, collections.abc.MutableSequence):
        return [from_value(v, item) for v in _seq(value, "a list")]
    if origin is tuple:
        items = _seq(value, "a list")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(from_value(v, args[0]) for v in items)
        if len(args) != len(items):  # tuple[()] has no args, and no items
            raise BadValue(f"expected {len(args)} items, got {len(items)}")
        return tuple(from_value(v, a) for v, a in zip(items, args, strict=True))
    if origin in (set, frozenset, collections.abc.Set, collections.abc.MutableSet):
        made = {from_value(v, item) for v in _seq(value, "a list")}
        return frozenset(made) if origin is frozenset else made
    if origin in (dict, collections.abc.Mapping, collections.abc.MutableMapping):
        if not isinstance(value, dict):
            raise BadValue(f"expected a mapping, got {_kind(value)}")
        key, val = (args + (Any, Any))[:2] if args else (Any, Any)
        return {from_value(k, key): from_value(v, val) for k, v in value.items()}
    if isinstance(origin, type) and issubclass(origin, EventedList):
        return EventedList(from_value(v, item) for v in _seq(value, "a list"))
    if isinstance(origin, type) and issubclass(origin, EventedSet):
        return EventedSet(from_value(v, item) for v in _seq(value, "a list"))
    if isinstance(origin, type) and issubclass(origin, EventedDict):
        return EventedDict(from_value(value, dict[args] if args else dict))
    if isinstance(origin, type):
        return _class(value, origin)
    return _untyped(value)


def _class(value, cls: type):
    if cls is Link or _is_syncable(cls):
        if isinstance(value, _core.Tagged) and value.tag == LINK and isinstance(value.value, str):
            try:
                return Link(value.value)
            except ValueError as e:
                raise BadValue(str(e)) from None
        raise BadValue(f"expected a link, got {_kind(value)}")
    if hasattr(cls, "synq_from_value"):
        try:
            return cls.synq_from_value(_untyped(value))
        except BadValue:
            raise
        except Exception as e:
            raise BadValue(f"{cls.__name__}.synq_from_value: {e}") from None
    if issubclass(cls, enum.Enum):
        try:
            return cls(value)
        except ValueError:
            raise BadValue(f"{value!r} isn't a {cls.__name__}") from None
    if _tagged_primitive(cls):
        if isinstance(value, _core.Tagged):
            if value.tag != tag_of(cls):
                raise BadValue(f"expected !{tag_of(cls)}, got !{value.tag}")
            value = value.value
        base = next(b for b in (str, int, float) if issubclass(cls, b))
        return cls(_class(value, base))
    if cls is bool:
        if isinstance(value, bool):
            return value
    elif cls is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif cls is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    elif cls is str:
        if isinstance(value, str):
            return value
    elif cls in (bytes, bytearray):
        if isinstance(value, bytes):
            return cls(value)
    elif dataclasses.is_dataclass(cls):
        return _dataclass(value, cls)
    elif issubclass(cls, EventedList):
        return EventedList(_untyped(v) for v in _seq(value, "a list"))
    elif issubclass(cls, EventedSet):
        return EventedSet(_untyped(v) for v in _seq(value, "a list"))
    elif issubclass(cls, EventedDict):
        return EventedDict(from_value(value, dict))
    elif cls in (list, tuple, set, frozenset):
        return cls(_untyped(v) for v in _seq(value, "a list"))
    elif cls is dict or cls is collections.abc.Mapping:
        return from_value(value, dict[Any, Any])
    else:
        raise BadValue(f"SpiriSynq doesn't know how to make a {cls.__name__}")
    raise BadValue(f"expected {cls.__name__}, got {_kind(value)}")


def _dataclass(value, cls: type):
    if isinstance(value, _core.Tagged):
        if value.tag != tag_of(cls):
            raise BadValue(f"expected !{tag_of(cls)}, got !{value.tag}")
        value = value.value
    if not isinstance(value, dict):
        raise BadValue(f"expected a {cls.__name__} mapping, got {_kind(value)}")
    hints = hints_of(cls)
    known = {f.name: f for f in wire_fields(cls)}
    kwargs = {}
    for name, v in value.items():
        if name not in known:
            continue  # a newer peer's field: not ours to set
        try:
            kwargs[name] = from_value(v, hints.get(name, Any))
        except BadValue as e:
            raise BadValue(f"{cls.__name__}.{name}: {e}") from None
    init = {k: v for k, v in kwargs.items() if known[k].init}
    try:
        made = cls(**init)
    except TypeError as e:
        raise BadValue(f"can't make a {cls.__name__}: {e}") from None
    for name, v in kwargs.items():
        if not known[name].init:
            object.__setattr__(made, name, v)
    return made


def hints_of(obj) -> dict:
    """Resolved annotations, or the raw ones where they can't be resolved."""
    try:
        return typing.get_type_hints(obj)
    except Exception:
        return dict(getattr(obj, "__annotations__", {}))
