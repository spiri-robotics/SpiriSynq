"""Python values to the core's values and back, guided by annotations."""

import collections.abc
import enum
from dataclasses import dataclass, field
from typing import Any, Literal, Required, TypeVar

import pytest
from psygnal.containers import EventedDict, EventedList, EventedSet

from SpiriSynq import _core
from SpiriSynq.link import Link, ObjectNotFound
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.values import BadValue, from_value, register, to_value

T = TypeVar("T")


class Mode(enum.Enum):
    A = "a"


class Frame(str):
    yaml_tag = "!Frame"


class Count(int):
    yaml_tag = "!Count"


class Ratio(float):
    yaml_tag = "!Ratio"


class Flag(int):  # a primitive subclass without a tag
    pass


@dataclass
class Point:
    x: float = 0.0
    y: float = 0.0
    _private: int = 0
    later: int = field(default=0, init=False)


@dataclass
class Other:
    x: float = 0.0


register(Point)


class Celsius:
    def __init__(self, degrees):
        self.degrees = degrees

    def synq_to_value(self):
        return {"c": self.degrees}

    @classmethod
    def synq_from_value(cls, value):
        if "c" not in value:
            raise BadValue("no degrees")
        return cls(value["c"])


class Broken:
    @classmethod
    def synq_from_value(cls, value):
        raise KeyError("x")


@dataclass
class Camera(SyncableObject):
    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000020:1"


def test_to_value():
    assert to_value(Mode.A) == "a"
    assert to_value(Frame("map")) == _core.Tagged("Frame", "map")
    assert type(to_value(Flag(3))) is int
    assert to_value(bytearray(b"a")) == b"a"
    assert to_value(memoryview(b"a")) == b"a"
    assert to_value((1, 2)) == [1, 2]
    assert to_value({3, 1}) == [1, 3]
    assert sorted(to_value({1, "a"}), key=str) == [1, "a"]
    assert to_value(EventedDict({"a": 1})) == {"a": 1}
    assert to_value(EventedList([1])) == [1]
    assert to_value(collections.UserList([1])) == [1]
    point = Point(1.0, 2.0)
    point.later = 3
    assert to_value(point) == _core.Tagged("Point", {"x": 1.0, "y": 2.0, "later": 3})
    assert to_value(Link("a/b")) == _core.Tagged("Link", "a/b")
    assert to_value(ObjectNotFound("a/b")) == _core.Tagged("Link", "a/b")
    assert to_value(Celsius(20)) == {"c": 20}
    tagged = _core.Tagged("X", 1)
    assert to_value(tagged) is tagged
    with pytest.raises(TypeError, match="can't be sent"):
        to_value(object())


def test_from_value_primitives():
    assert from_value(1, float) == 1.0 and type(from_value(1, float)) is float
    assert from_value(True, bool) is True
    assert from_value(b"a", bytearray) == bytearray(b"a")
    for value, hint in [(True, int), (1, bool), (1, str), ("a", bytes), (True, float)]:
        with pytest.raises(BadValue):
            from_value(value, hint)
    assert from_value(None, None) is None
    with pytest.raises(BadValue, match="expected None"):
        from_value(1, type(None))
    assert from_value("a", Literal["a", "b"]) == "a"
    with pytest.raises(BadValue, match="one of"):
        from_value("c", Literal["a", "b"])
    assert from_value("a", Mode) is Mode.A
    with pytest.raises(BadValue, match="isn't a Mode"):
        from_value("z", Mode)
    assert from_value(5, T) == 5
    assert from_value(5, "forward") == 5


def test_from_value_tagged_primitives():
    assert from_value(_core.Tagged("Frame", "map"), Frame) == Frame("map")
    assert isinstance(from_value("map", Frame), Frame)
    assert from_value(_core.Tagged("Count", 3), Count) == 3
    assert from_value(1, Ratio) == 1.0
    with pytest.raises(BadValue, match="expected !Frame"):
        from_value(_core.Tagged("Other", "x"), Frame)


def test_from_value_collections():
    assert from_value([1, 2], list[int]) == [1, 2]
    assert from_value([1, 2], tuple[int, ...]) == (1, 2)
    assert from_value([1, "a"], tuple[int, str]) == (1, "a")
    with pytest.raises(BadValue, match="expected 2 items"):
        from_value([1], tuple[int, str])
    assert from_value([], tuple[()]) == ()
    assert from_value([1], set[int]) == {1}
    assert from_value([1], frozenset[int]) == frozenset({1})
    assert from_value({1: "a"}, dict[int, str]) == {1: "a"}
    assert from_value({1: "a"}, collections.abc.Mapping) == {1: "a"}
    with pytest.raises(BadValue, match="expected a mapping"):
        from_value([1], dict[str, int])
    with pytest.raises(BadValue, match="expected a list"):
        from_value({}, list[int])
    assert from_value([1], EventedList[int]) == EventedList([1])
    assert from_value([1], EventedSet[int]) == EventedSet([1])
    assert from_value({"a": 1}, EventedDict[str, int]) == EventedDict({"a": 1})
    assert isinstance(from_value([1], EventedList), EventedList)
    assert isinstance(from_value([1], EventedSet), EventedSet)
    assert isinstance(from_value({"a": 1}, EventedDict), EventedDict)
    assert from_value([1, 2], tuple) == (1, 2)
    assert from_value({"a": [1]}, dict) == {"a": [1]}
    with pytest.raises(BadValue, match="doesn't know how to make a UserList"):
        from_value([1], collections.UserList[int])
    assert from_value([1], Required[int]) == [1]  # a special form it can't use: as it is


def test_from_value_dataclasses():
    tagged = _core.Tagged("Point", {"x": 1, "y": 2.0, "later": 4, "new": "ignored"})
    point = from_value(tagged, Point)
    assert (point.x, point.y, point.later) == (1.0, 2.0, 4)
    assert from_value({"x": 1.0}, Point) == Point(1.0)
    with pytest.raises(BadValue, match="expected !Point"):
        from_value(_core.Tagged("Other", {}), Point)
    with pytest.raises(BadValue, match="mapping"):
        from_value(1, Point)
    with pytest.raises(BadValue, match="Point.x"):
        from_value({"x": "a"}, Point)

    @dataclass
    class Needs:
        a: int

    with pytest.raises(BadValue, match="can't make a Needs"):
        from_value({}, Needs)


def test_from_value_unions():
    assert from_value(None, int | None) is None
    assert from_value(1, int | str) == 1
    assert from_value(_core.Tagged("Point", {"x": 1.0}), Other | Point) == Point(1.0)
    with pytest.raises(BadValue, match="fits none of"):
        from_value("a", int | float)
    # An untagged value goes to an untagged member before a tagged primitive.
    plain = from_value("a/b", Frame | str)
    assert plain == "a/b" and type(plain) is str
    assert type(from_value(_core.Tagged("Frame", "ECEF"), Frame | str)) is Frame
    assert type(from_value("ECEF", Frame | None)) is Frame  # nothing else takes it


def test_from_value_untyped():
    assert from_value(_core.Tagged("Link", "a/b")) == Link("a/b")
    assert from_value(_core.Tagged("Point", {"x": 1.0})) == Point(1.0)
    odd = _core.Tagged("Point", "not a mapping")
    assert from_value(odd) == odd
    unknown = _core.Tagged("Unknown", 1)
    assert from_value([unknown], Any) == [unknown]
    assert from_value({"a": [1]}, object) == {"a": [1]}


def test_from_value_links():
    assert from_value(_core.Tagged("Link", "a/b"), Camera) == Link("a/b")
    assert from_value(_core.Tagged("Link", "a/b"), Link) == Link("a/b")
    with pytest.raises(BadValue, match="expected a link"):
        from_value("a/b", Camera)
    with pytest.raises(BadValue, match="Invalid link"):
        from_value(_core.Tagged("Link", "a/*"), Camera)


def test_from_value_custom_classes():
    assert from_value({"c": 5}, Celsius).degrees == 5
    with pytest.raises(BadValue, match="no degrees"):
        from_value({}, Celsius)
    with pytest.raises(BadValue, match="Broken.synq_from_value"):
        from_value({}, Broken)
    with pytest.raises(BadValue, match="doesn't know how to make"):
        from_value(1, complex)


def test_more_edges():
    from SpiriSynq.schema import JsonSchema
    from SpiriSynq.values import hints_of

    assert from_value({"a": 1}, JsonSchema) == {"a": 1}  # not a class: as it is
    with pytest.raises(BadValue, match="got !X"):
        from_value(_core.Tagged("X", 1), int)

    class Unresolvable:
        x: "NoSuchName"  # noqa: F821

    assert hints_of(Unresolvable) == {"x": "NoSuchName"}
