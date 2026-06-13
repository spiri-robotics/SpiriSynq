"""
High-level integration tests for SpiriSynq session.
These tests simulate real-world usage patterns with two communicating sessions.
"""

import time
from dataclasses import dataclass, field
from typing import ClassVar

import pytest
from psygnal import SignalGroupDescriptor
from psygnal.containers import EventedList, EventedDict

from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session


import threading
from loguru import logger
import sys

logger.configure(handlers=[{"sink": sys.stderr, "level": "TRACE"}])


@pytest.fixture(autouse=True, scope="session")
def dump_threads_on_exit():
    yield
    print("\n=== Threads still alive ===")
    for t in threading.enumerate():
        print(f"  {t.name!r}  daemon={t.daemon}")


@pytest.fixture(autouse=True)
def close_test_sessions():
    """Close any Sessions created during the test.

    pytest keeps failing-test frames alive (for traceback display), which
    prevents __del__ from running and leaves zenoh threads/queryables active.
    Explicitly calling .close() shuts down the zenoh session regardless of
    whether the Python object is GC'd.
    """
    from SpiriSynq.shutdown import _live_sessions
    before = set(_live_sessions.keys())
    yield
    for sid, session in list(_live_sessions.items()):
        if sid not in before:
            session.close()


def _wait_for(predicate, timeout=1.0, interval=0.01):
    """Poll until predicate returns True or timeout expires."""
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_basic_field_synchronization():
    """
    As a developer, I can publish a simple object and receive updates
    on another session, with primitive fields automatically synced.
    """

    @dataclass
    class SimpleData(SyncableObject):
        speed: float = 0.0
        name: str = ""

    # Create an authoritative object (internal session)
    obj = SimpleData("test/obj", synq_authoritive=True, speed=42.5, name="test")
    # Receive the object on a separate session
    session_b = Session()
    remote = SimpleData.from_topic(obj.synq_absolute_path, session=session_b)

    assert obj.synq_session != remote.synq_session, "local and remote obj use same session, invalid test"
    # Initial state should match, no waiting
    assert remote.speed == 42.5
    assert remote.name == "test"
    assert isinstance(remote, SimpleData)

    # Change a field on the published side
    obj.speed = 99.9
    assert _wait_for(lambda: remote.speed == 99.9), "Timeout waiting for speed update"

    # Change a field on the remote side (should propagate back)
    remote.name = "updated"
    assert _wait_for(lambda: obj.name == "updated"), "Timeout waiting for name update"


def test_nested_dataclass_synchronization():
    """
    Nested SyncableObjects should propagate changes across the network,
    enabling hierarchical data models.
    """

    @dataclass
    class Inner:
        events: ClassVar[SignalGroupDescriptor] = SignalGroupDescriptor()
        value: int = 0

    @dataclass
    class Outer(SyncableObject):
        inner: Inner | None = None
        label: str = ""

    # Authoritative object
    obj = Outer(
        "test/nested", synq_authoritive=True, inner=Inner(value=10), label="outer"
    )
    session_b = Session()
    remote = Outer(synq_session=session_b, synq_topic=obj.synq_absolute_path)

    assert remote.inner.value == 10
    assert remote.label == "outer"

    # Update nested field
    obj.inner.value = 20
    assert _wait_for(lambda: remote.inner.value == 20), (
        "Timeout waiting for nested value update"
    )

    # Update outer field
    remote.label = "changed"
    assert _wait_for(lambda: obj.label == "changed"), (
        "Timeout waiting for outer label update"
    )


def test_optional_nested_dataclass():
    """
    Test a nested dataclass starting from None and becoming a real dataclass
    """

    @dataclass
    class Inner:
        events: ClassVar[SignalGroupDescriptor] = SignalGroupDescriptor()
        value: int = 0

    @dataclass
    class Outer(SyncableObject):
        inner: Inner | None = None
        label: str = ""

    # Authoritative object
    obj = Outer("test/nested", synq_authoritive=True, label="outer")
    session_b = Session()
    remote = Outer(synq_session=session_b, synq_topic=obj.synq_absolute_path)

    obj.inner = Inner(value=10)

    assert _wait_for(lambda: remote.inner is not None)
    assert remote.inner.value == 10
    assert remote.label == "outer"

    # Update nested field
    obj.inner.value = 20
    assert _wait_for(lambda: remote.inner.value == 20), (
        "Timeout waiting for nested value update"
    )

    # Update outer field
    remote.label = "changed"
    assert _wait_for(lambda: obj.label == "changed"), (
        "Timeout waiting for outer label update"
    )


def test_list_topics():
    """
    The list_topics method should yield topic metadata dicts for discovered topics.
    """
    @dataclass
    class TestData(SyncableObject):
        value: int = 0

    # Authoritative object
    obj = TestData("test/list_topics", synq_authoritive=True, value=42)
    path = obj.synq_absolute_path

    session_b = Session()

    # Test discovery and metadata integrity via prefix filter (avoids noise from
    # stale queryables of other tests that pytest keeps alive in tracebacks).
    assert _wait_for(
        lambda: any(
            t is not None and t.get("topic") == path and "!TestData" in t.get("classes", [])
            for t in session_b.list_topics(prefix=path)
        ),
        timeout=3,
    ), f"Timeout: Topic with correct path and type not discovered. {path}: TestData"

    # Test type filtering
    assert _wait_for(
        lambda: any(
            t is not None and "!TestData" in t.get("classes", [])
            for t in session_b.list_topics(type_filter="TestData")
        ),
        timeout=2,
    ), "Timeout: Topic not found via type filter."

    # Test general existence
    assert _wait_for(lambda: any(True for _ in session_b.list_topics()), timeout=2), (
        "Timeout: No topics discovered at all."
    )


def test_evented_container_synchronization():
    """
    Evented containers (EventedList, EventedDict) allow collection mutations
    to be automatically synchronized across sessions.
    """

    @dataclass
    class WithContainers(SyncableObject):
        items: EventedList = field(default_factory=EventedList)
        mapping: EventedDict = field(default_factory=EventedDict)

    session_b = Session()

    # Authoritative object
    obj = WithContainers(
        "test/containers",
        synq_authoritive=True,
        items=EventedList([1, 2, 3]),
        mapping=EventedDict({"a": 1}),
    )
    remote = WithContainers(synq_session=session_b, synq_topic=obj.synq_absolute_path)

    # Initial state
    assert remote.items == [1, 2, 3]
    assert remote.mapping == {"a": 1}

    # Mutate list on source
    obj.items.append(4)
    assert _wait_for(lambda: remote.items == [1, 2, 3, 4]), (
        "Timeout waiting for list update"
    )

    # Mutate dict on remote
    remote.mapping["b"] = 2
    assert _wait_for(lambda: obj.mapping == {"a": 1, "b": 2}), (
        "Timeout waiting for dict update"
    )


def test_raw_bytes_field():
    """
    Raw bytes fields are transmitted as binary payloads (APPLICATION_OCTET_STREAM)
    without YAML serialization overhead, useful for large binary data.
    """

    @dataclass
    class WithBytes(SyncableObject):
        data: bytes = b""

    session_b = Session()

    obj = WithBytes("test/bytes", synq_authoritive=True, data=b"hello\x00world")
    remote = WithBytes(synq_session=session_b, synq_topic=obj.synq_absolute_path)

    assert remote.data == b"hello\x00world"

    # Update bytes
    obj.data = b"updated"
    assert _wait_for(lambda: remote.data == b"updated"), (
        "Timeout waiting for bytes update"
    )


def test_large_bytes_sync():
    """
    Sync a 10 MiB bytes field to verify that large binary payloads are handled correctly.
    """

    @dataclass
    class LargeBytes(SyncableObject):
        data: bytes = b""
        skip_rehydrate = {"data"}

    session_b = Session()

    # 10 MiB of data
    size = 10 * 1024 * 1024
    large_data = b"x" * size

    obj = LargeBytes("test/large_bytes", synq_authoritive=True)
    remote = LargeBytes(synq_session=session_b, synq_topic=obj.synq_absolute_path)

    # Update with different pattern
    new_data = b"y" * size
    obj.data = new_data
    assert _wait_for(lambda: remote.data == new_data, timeout=5.0), (
        "Timeout waiting for large bytes update"
    )


# --- warn_non_evented tests ---

def _capture_warnings(fn):
    """Run fn() and return any loguru WARNING+ messages emitted during it."""
    messages = []
    handler_id = logger.add(messages.append, level="WARNING")
    try:
        fn()
    finally:
        logger.remove(handler_id)
    return messages


def test_warns_non_evented_non_frozen_nested_dataclass():
    """A non-evented, non-frozen nested dataclass in a SyncableObject should produce a warning."""

    @dataclass
    class MutableNested:
        value: float = 0.0

    @dataclass
    class ObjWithMutableNested(SyncableObject):
        nested: MutableNested | None = None

    messages = _capture_warnings(
        lambda: ObjWithMutableNested("test/warn_mutable_nested", synq_authoritive=True)
    )
    assert any("non-evented" in str(m) for m in messages), (
        f"Expected a non-evented warning but got: {messages}"
    )


def test_no_warn_for_frozen_nested_dataclass():
    """A frozen nested dataclass should not trigger a warning."""

    @dataclass(frozen=True)
    class FrozenNested:
        value: float = 0.0

    @dataclass
    class ObjWithFrozenNested(SyncableObject):
        nested: FrozenNested | None = None

    messages = _capture_warnings(
        lambda: ObjWithFrozenNested("test/warn_frozen_nested", synq_authoritive=True)
    )
    assert not any("non-evented" in str(m) for m in messages), (
        f"Expected no non-evented warning but got: {messages}"
    )


def test_warn_non_evented_false_suppresses_warning():
    """warn_non_evented = False on the SyncableObject subclass suppresses the warning."""

    @dataclass
    class MutableNested:
        value: float = 0.0

    @dataclass
    class SilentObj(SyncableObject):
        warn_non_evented = False
        nested: MutableNested | None = None

    messages = _capture_warnings(
        lambda: SilentObj("test/warn_silent", synq_authoritive=True)
    )
    assert not any("non-evented" in str(m) for m in messages), (
        f"Expected no non-evented warning but got: {messages}"
    )
