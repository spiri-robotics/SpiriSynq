"""
High-level integration tests for SpiriSynq session.
These tests simulate real-world usage patterns with two communicating sessions.
"""
import time
from dataclasses import dataclass, field

import pytest
from psygnal.containers import EventedList, EventedDict

from SpiriSynq.session import Session, SyncableObject

import threading
import traceback
import sys
import pytest

import threading
import traceback
import sys
import pytest
import gc
import weakref
import inspect

@pytest.fixture(autouse=True, scope="session")
def dump_threads_on_exit():
    gc.collect()
    gc.collect()
    yield
    print("\n=== Threads still alive ===")
    for t in threading.enumerate():
        print(f"  {t.name!r}  daemon={t.daemon}")

def _wait_for(predicate, timeout=1.0, interval=0.01):
    """Poll until predicate returns True or timeout expires."""
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


import gc
import weakref

def test_session_gc():
    @dataclass
    class SimpleData(SyncableObject):
        speed: float = 0.0
        name: str = ""

    session_a = Session()
    session_b = Session()

    obj = SimpleData(speed=42.5, name="test")
    path = session_a.publish_synced_object("test/obj", obj, authoritative=True)
    remote_obj = session_b.receive_synced_object(path)

    ref_session_a = weakref.ref(session_a)
    ref_session_b = weakref.ref(session_b)
    ref_obj = weakref.ref(obj)
    ref_remote_obj = weakref.ref(remote_obj)

    del session_a, session_b, obj, remote_obj
    gc.collect()

    still_alive = {
        "session_a": ref_session_a(),
        "session_b": ref_session_b(),
        "obj": ref_obj(),
        "remote_obj": ref_remote_obj(),
    }
    still_alive = {k: v for k, v in still_alive.items() if v is not None}

    for name, obj in still_alive.items():
        print(f"\n=== Referrers of {name} ===")
        import objgraph
        objgraph.show_most_common_types(limit=5)
        print("\n--- Direct referrers ---")
        for ref in gc.get_referrers(obj):
            if type(ref).__name__ == 'cell':
                for ref2 in gc.get_referrers(ref):
                    if inspect.isfunction(ref2):
                        print(f"  closure: {ref2.__qualname__} at {inspect.getfile(ref2)}:{ref2.__code__.co_firstlineno}")

    assert not still_alive, f"Objects not garbage collected: {list(still_alive.keys())}"


def test_basic_field_synchronization():
    """
    As a developer, I can publish a simple object and receive updates
    on another session, with primitive fields automatically synced.
    """
    @dataclass
    class SimpleData(SyncableObject):
        speed: float = 0.0
        name: str = ""

    # Create two independent sessions (they'll communicate via default zenoh config)
    session_a = Session()
    session_b = Session()

    # Publish an object from session A
    obj = SimpleData(speed=42.5, name="test")
    path = session_a.publish_synced_object("test/obj", obj, authoritative=True)

    # Receive the object on session B (this also subscribes to future updates)
    remote_obj = session_b.receive_synced_object(path)

    # Initial state should match
    assert remote_obj.speed == 42.5
    assert remote_obj.name == "test"

    # Change a field on the published side
    obj.speed = 99.9
    # Wait for the update to propagate
    assert _wait_for(lambda: remote_obj.speed == 99.9), "Timeout waiting for speed update"

    # Change a field on the remote side (should propagate back)
    remote_obj.name = "updated"
    assert _wait_for(lambda: obj.name == "updated"), "Timeout waiting for name update"



def test_nested_dataclass_synchronization():
    """
    Nested SyncableObjects should propagate changes across the network,
    enabling hierarchical data models.
    """
    @dataclass
    class Inner(SyncableObject):
        value: int = 0

    @dataclass
    class Outer(SyncableObject):
        inner: Inner = field(default_factory=Inner)
        label: str = ""

    session_a = Session()
    session_b = Session()

    obj = Outer(inner=Inner(value=10), label="outer")
    path = session_a.publish_synced_object("test/nested", obj, authoritative=True)

    remote = session_b.receive_synced_object(path)

    assert remote.inner.value == 10
    assert remote.label == "outer"

    # Update nested field
    obj.inner.value = 20
    assert _wait_for(lambda: remote.inner.value == 20), "Timeout waiting for nested value update"

    # Update outer field
    remote.label = "changed"
    assert _wait_for(lambda: obj.label == "changed"), "Timeout waiting for outer label update"

    session_a.close()
    session_b.close()

def test_evented_container_synchronization():
    """
    Evented containers (EventedList, EventedDict) allow collection mutations
    to be automatically synchronized across sessions.
    """
    @dataclass
    class WithContainers(SyncableObject):
        items: EventedList = field(default_factory=EventedList)
        mapping: EventedDict = field(default_factory=EventedDict)

    session_a = Session()
    session_b = Session()

    obj = WithContainers(items=EventedList([1, 2, 3]), mapping=EventedDict({"a": 1}))
    path = session_a.publish_synced_object("test/containers", obj, authoritative=True)

    remote = session_b.receive_synced_object(path)

    # Initial state
    assert remote.items == [1, 2, 3]
    assert remote.mapping == {"a": 1}

    # Mutate list on source
    obj.items.append(4)
    assert _wait_for(lambda: remote.items == [1, 2, 3, 4]), "Timeout waiting for list update"

    # Mutate dict on remote
    remote.mapping["b"] = 2
    assert _wait_for(lambda: obj.mapping == {"a": 1, "b": 2}), "Timeout waiting for dict update"


def test_raw_bytes_field():
    """
    Raw bytes fields are transmitted as binary payloads (APPLICATION_OCTET_STREAM)
    without YAML serialization overhead, useful for large binary data.
    """
    @dataclass
    class WithBytes(SyncableObject):
        data: bytes = b""

    session_a = Session()
    session_b = Session()

    obj = WithBytes(data=b"hello\x00world")
    path = session_a.publish_synced_object("test/bytes", obj, authoritative=True)

    remote = session_b.receive_synced_object(path)

    assert remote.data == b"hello\x00world"

    # Update bytes
    obj.data = b"updated"
    assert _wait_for(lambda: remote.data == b"updated"), "Timeout waiting for bytes update"

def test_rehydrate_endpoint():
    """
    Authoritative objects expose a rehydrate queryable that returns the full
    current state, enabling late‑joining participants to catch up.
    """
    @dataclass
    class RehydrateExample(SyncableObject):
        value: int = 0
        text: str = ""

    session_a = Session()
    session_b = Session()

    obj = RehydrateExample(value=100, text="initial")
    path = session_a.publish_synced_object("test/rehydrate", obj, authoritative=True)

    # Simulate a late joiner that only wants the current state without subscribing
    snapshot = session_b.receive_synced_object(path, receive=False, publish=False)
    assert snapshot.value == 100
    assert snapshot.text == "initial"

    # The snapshot is a plain object, not linked for updates
    obj.value = 200
    # Wait a moment to ensure no accidental propagation
    time.sleep(0.1)
    # snapshot should stay unchanged
    assert snapshot.value == 100
