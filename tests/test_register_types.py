from conftest import zenoh_test_config
"""
Unit tests for Session.register_type_recursive.

These are pure unit tests — they create a Session to get a type registry but
don't exercise zenoh network I/O.
"""

import pytest
from dataclasses import dataclass
from typing import Optional

from SpiriSynq.session import Session
from SpiriSynq.syncable_objects import SyncableObject


@pytest.fixture
def session():
    s = Session(config=zenoh_test_config())
    yield s
    s.close()


def _representable(session, t):
    rep = session.type_registry.representer
    return any(mro_t in rep.yaml_representers for mro_t in t.__mro__)


def _constructable(session, t):
    tag = getattr(t, "yaml_tag", None)
    if tag is None:
        return False
    return tag in session.type_registry.constructor.yaml_constructors


# --- primitives are already handled ---

def test_primitives_already_representable(session):
    """int/str/float/bool are in SafeRepresenter from the start; no yaml_tag is set on them."""
    for t in (int, str, float, bool, type(None)):
        assert _representable(session, t), f"{t} should already be representable"
        assert not hasattr(t, "yaml_tag"), f"{t} should not get a yaml_tag monkeypatched onto it"


# --- plain dataclass without to_yaml/from_yaml ---

def test_plain_dataclass_is_registered(session):
    """A plain dataclass gets a yaml_tag assigned and both representer+constructor entries added."""

    @dataclass
    class Point:
        x: float = 0.0
        y: float = 0.0

    assert not _representable(session, Point)
    session.register_type_recursive(Point)
    assert _representable(session, Point)
    assert _constructable(session, Point)
    assert Point.yaml_tag == "!Point"


def test_plain_dataclass_roundtrips(session):
    """After registration, a plain dataclass serialises and deserialises correctly."""

    @dataclass
    class Color:
        r: int = 0
        g: int = 0
        b: int = 0

    session.register_type_recursive(Color)
    original = Color(r=255, g=128, b=0)
    yaml_str = session.type_registry.dumps(original)
    restored = session.type_registry.load(yaml_str)
    assert isinstance(restored, Color)
    assert restored.r == 255 and restored.g == 128 and restored.b == 0


# --- nested types in field annotations are walked ---

def test_nested_dataclass_registered_transitively(session):
    """A dataclass nested inside another's field annotation is registered automatically."""

    @dataclass
    class Inner:
        value: int = 0

    @dataclass
    class Outer:
        inner: Inner = None

    session.register_type_recursive(Outer)
    assert _representable(session, Inner), "Inner should be registered transitively"
    assert _constructable(session, Inner)


def test_optional_inner_type_registered(session):
    """Optional[Foo] causes Foo to be registered."""

    @dataclass
    class Foo:
        x: float = 0.0

    @dataclass
    class Bar:
        foo: Optional[Foo] = None

    session.register_type_recursive(Bar)
    assert _representable(session, Foo)


def test_union_both_branches_registered(session):
    """Foo | Bar causes both branches to be registered."""

    @dataclass
    class Alpha:
        v: int = 0

    @dataclass
    class Beta:
        v: str = ""

    @dataclass
    class Container:
        item: Alpha | Beta | None = None

    session.register_type_recursive(Container)
    assert _representable(session, Alpha)
    assert _representable(session, Beta)


# --- SyncableObject subclass ---

def test_syncable_object_registered(session):
    """A SyncableObject subclass is fully registered with representer and constructor."""

    @dataclass
    class MyObj(SyncableObject):
        value: int = 0

    session.register_type_recursive(MyObj)
    assert _representable(session, MyObj)
    assert _constructable(session, MyObj)


def test_syncable_object_skip_sync_fields_not_walked(session):
    """Fields in synq_skip_sync (e.g. synq_session) don't cause their types to be registered."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    # Session itself has no to_yaml/from_yaml but ruamel would auto-register it;
    # the important thing is that synq_session is in synq_skip_sync, so
    # register_type_recursive must not choke trying to register Session's internals.
    # We verify indirectly: if skip logic is broken, this raises.
    session.register_type_recursive(Obj)


def test_syncable_object_nested_dataclass_registered(session):
    """A nested dataclass in a SyncableObject field annotation is registered."""

    @dataclass
    class Config:
        rate: float = 1.0
        label: str = ""

    @dataclass
    class Device(SyncableObject):
        config: Config | None = None

    session.register_type_recursive(Device)
    assert _representable(session, Config)
    assert _constructable(session, Config)


# --- idempotency ---

def test_registration_is_idempotent(session):
    """Calling register_type_recursive twice on the same type does not raise."""

    @dataclass
    class Thing:
        x: int = 0

    session.register_type_recursive(Thing)
    session.register_type_recursive(Thing)  # should not raise
    assert _representable(session, Thing)


def test_primitive_fields_not_double_registered(session):
    """A SyncableObject with int/str fields doesn't monkeypatch those built-in types."""

    @dataclass
    class Simple(SyncableObject):
        count: int = 0
        name: str = ""

    session.register_type_recursive(Simple)
    assert not hasattr(int, "yaml_tag")
    assert not hasattr(str, "yaml_tag")
