from conftest import zenoh_test_config
"""
Tests for Session.from_topic_untyped -- mirroring an object whose dataclass
isn't importable in this process.
"""

import time
from dataclasses import dataclass

import pytest

from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session


@pytest.fixture(autouse=True)
def close_test_sessions():
    from SpiriSynq.shutdown import _live_sessions
    before = set(_live_sessions.keys())
    yield
    for sid, session in list(_live_sessions.items()):
        if sid not in before:
            session.close()


def _wait_for(predicate, timeout=1.0, interval=0.01):
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_from_topic_untyped_mirrors_fields():
    """A mirror built without the real dataclass should still have the same field values."""

    @dataclass
    class UntypedProbe(SyncableObject):
        speed: float = 0.0
        label: str = "hi"

    obj = UntypedProbe("test/untyped_basic", synq_authoritive=True, speed=7.0, label="hello")

    session_b = Session(config=zenoh_test_config())
    remote = session_b.from_topic_untyped(obj.synq_absolute_path)

    assert not isinstance(remote, UntypedProbe)
    assert remote.speed == 7.0
    assert remote.label == "hello"


def test_from_topic_untyped_receives_updates():
    """Field changes on the authoritative side should propagate to the generic mirror."""

    @dataclass
    class UntypedProbe2(SyncableObject):
        counter: int = 0

    obj = UntypedProbe2("test/untyped_updates", synq_authoritive=True)

    session_b = Session(config=zenoh_test_config())
    remote = session_b.from_topic_untyped(obj.synq_absolute_path)

    obj.counter = 42
    assert _wait_for(lambda: remote.counter == 42), "Timeout: remote value not propagated"


def test_from_topic_untyped_caches_class_per_tags():
    """Repeated calls for objects of the same class should reuse the synthesized class."""

    @dataclass
    class UntypedProbe3(SyncableObject):
        value: int = 0

    obj_a = UntypedProbe3("test/untyped_cache_a", synq_authoritive=True)
    obj_b = UntypedProbe3("test/untyped_cache_b", synq_authoritive=True)

    session_b = Session(config=zenoh_test_config())
    remote_a = session_b.from_topic_untyped(obj_a.synq_absolute_path)
    remote_b = session_b.from_topic_untyped(obj_b.synq_absolute_path)

    assert type(remote_a) is type(remote_b)
