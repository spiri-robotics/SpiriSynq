"""
High-level integration tests for SpiriSynq session.
These tests simulate real-world usage patterns with two communicating sessions.
"""
import time
from dataclasses import dataclass, field

import pytest
from psygnal.containers import EventedList, EventedDict, EventedSet

from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session

import threading
import gc
import weakref
import inspect
import traceback
import sys
from loguru import logger


def _format_referrers(instance):
    """Return a readable referrer chain for a live object."""
    if instance is None:
        return "(object already collected)"
    lines = []
    for ref in gc.get_referrers(instance):
        lines.append(f"  [{type(ref).__name__}] {repr(ref)[:300]}")
        if type(ref).__name__ == 'cell':
            for owner in gc.get_referrers(ref):
                if type(owner).__name__ == 'tuple':
                    for fn in gc.get_referrers(owner):
                        if inspect.isfunction(fn):
                            lines.append(
                                f"    ^ cell in closure: {fn.__qualname__}"
                                f" @ {inspect.getfile(fn)}:{fn.__code__.co_firstlineno}"
                            )
                elif inspect.isfunction(owner):
                    lines.append(
                        f"    ^ cell in function: {owner.__qualname__}"
                        f" @ {inspect.getfile(owner)}:{owner.__code__.co_firstlineno}"
                    )
        elif isinstance(ref, dict):
            for owner in gc.get_referrers(ref):
                if inspect.isclass(owner):
                    lines.append(f"    ^ dict owned by class: {owner.__qualname__}")
                elif 'SignalGroup' in type(owner).__name__:
                    lines.append(f"    ^ dict owned by SignalGroupDescriptor: {owner}")
    return "\n".join(lines) if lines else "  (no referrers found)"


@pytest.fixture(autouse=True, scope="session")
def dump_threads_on_exit():
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


def test_handlers_cleaned_up_when_object_goes_out_of_scope():
    """
    Non-authoritative handlers should be removed from the session when the remote
    object goes out of scope, via weakref.finalize.
    """
    @dataclass
    class SimpleData(SyncableObject):
        speed: float = 0.0
        name: str = ""

    session_b = Session()

    # Create authoritative object (internal session)
    obj = SimpleData("test/obj", synq_authoritive=True, speed=1.0, name="test")
    remote = SimpleData(synq_session=session_b, synq_topic=obj.synq_absolute_path)

    topic_path = obj.synq_absolute_path

    assert topic_path in session_b._handlers_non_authoritative, \
        "Non‑authoritative handler should be registered on session_b"

    ref_remote = weakref.ref(remote)

    del remote
    gc.collect()

    assert ref_remote() is None, "remote_obj not GC'd"

    assert topic_path not in session_b._handlers_non_authoritative, \
        "Handler not removed after remote object GC"


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
    remote = SimpleData(synq_session=session_b, synq_topic=obj.synq_absolute_path)

    # Initial state should match
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
    class Inner(SyncableObject):
        value: int = 0

    @dataclass
    class Outer(SyncableObject):
        inner: Inner = field(default_factory=Inner)
        label: str = ""

    # Authoritative object
    obj = Outer("test/nested", synq_authoritive=True, inner=Inner(value=10), label="outer")
    session_b = Session()
    remote = Outer(synq_session=session_b, synq_topic=obj.synq_absolute_path)

    assert remote.inner.value == 10
    assert remote.label == "outer"

    # Update nested field
    obj.inner.value = 20
    assert _wait_for(lambda: remote.inner.value == 20), "Timeout waiting for nested value update"

    # Update outer field
    remote.label = "changed"
    assert _wait_for(lambda: obj.label == "changed"), "Timeout waiting for outer label update"


def test_optional_nested_dataclass():
    """
    Test a nested dataclass starting from None and becoming a real dataclass
    """
    @dataclass
    class Inner(SyncableObject):
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
    assert _wait_for(lambda: remote.inner.value == 20), "Timeout waiting for nested value update"

    # Update outer field
    remote.label = "changed"
    assert _wait_for(lambda: obj.label == "changed"), "Timeout waiting for outer label update"


def test_nested_dataclass_separate_topic_init():
    """
    Nested SyncableObjects should be published as separate topics,
    enabling independent discovery and subscription.
    """
    @dataclass
    class Inner(SyncableObject):
        value: int = 0

    @dataclass
    class Outer(SyncableObject):
        inner: Inner = field(default_factory=Inner)
        label: str = ""

    # Authoritative object
    obj = Outer("test/nested_separate", synq_authoritive=True,
                label="outer", inner=Inner(value=10))
    outer_path = obj.synq_absolute_path
    session_b = Session()

    def _wait_for_inner():
        topics = list(session_b.list_topics())
        inner_path = outer_path + "/inner"
        return any(t.get('path') == inner_path and t.get('type') == 'Inner' for t in topics)
    assert _wait_for(_wait_for_inner), "Timeout waiting for inner topic metadata"

    # Collect metadata for both outer and inner
    topics = list(session_b.list_topics())
    outer_found = any(t['path'] == outer_path and t['type'] == 'Outer' for t in topics)
    inner_found = any(t['path'] == outer_path + "/inner" and t['type'] == 'Inner' for t in topics)
    assert outer_found, f"Outer topic not found in {topics}"
    assert inner_found, f"Inner topic not found in {topics}"

    # Receive inner object directly via its own path
    inner_path = outer_path + "/inner"
    inner_obj = Inner(synq_session=session_b, synq_topic=inner_path)
    assert isinstance(inner_obj, Inner)
    assert inner_obj.value == 10

    # Ensure changes to inner propagate via its own topic
    obj.inner.value = 20
    assert _wait_for(lambda: inner_obj.value == 20), "Timeout waiting for inner update via separate topic"


def test_nested_dataclass_separate_topic_runtime():
    """
    Nested SyncableObjects should be published as separate topics,
    enabling independent discovery and subscription (dynamic creation).
    """
    @dataclass
    class Inner(SyncableObject):
        value: int = 0

    @dataclass
    class Outer(SyncableObject):
        inner: Inner | None = None
        label: str = ""

    # Authoritative object
    obj = Outer("test/nested_separate", synq_authoritive=True, label="outer")
    outer_path = obj.synq_absolute_path
    session_b = Session()

    obj.inner = Inner(value=10)

    def _wait_for_inner():
        topics = list(session_b.list_topics())
        inner_path = outer_path + "/inner"
        return any(t.get('path') == inner_path and t.get('type') == 'Inner' for t in topics)
    assert _wait_for(_wait_for_inner), "Timeout waiting for inner topic metadata"

    # Collect metadata for both outer and inner
    topics = list(session_b.list_topics())
    logger.debug(topics)

    outer_found = any(t['path'] == outer_path and t['type'] == 'Outer' for t in topics)
    inner_found = any(t['path'] == outer_path + "/inner" and t['type'] == 'Inner' for t in topics)
    assert outer_found, f"Outer topic not found in {topics}"
    assert inner_found, f"Inner topic not found in {topics}"

    # Receive inner object directly via its own path
    inner_path = outer_path + "/inner"
    inner_obj = Inner(synq_session=session_b, synq_topic=inner_path)
    assert isinstance(inner_obj, Inner)
    assert inner_obj.value == 10

    # Ensure changes to inner propagate via its own topic
    obj.inner.value = 20
    assert _wait_for(lambda: inner_obj.value == 20), "Timeout waiting for inner update via separate topic"


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

    # Test discovery and metadata integrity together.
    assert _wait_for(
        lambda: any(
            t.get('path') == path and t.get('type') == 'TestData'
            for t in session_b.list_topics()
        ),
        timeout=3
    ), f"Timeout: Topic with correct path and type not discovered. {path}: TestData"

    # Test prefix filtering
    assert _wait_for(
        lambda: any(t.get('path') == path for t in session_b.list_topics(prefix=path)),
        timeout=2
    ), "Timeout: Topic not found via prefix filter."

    # Test type filtering
    assert _wait_for(
        lambda: any(t.get('type') == 'TestData' for t in session_b.list_topics(type_filter='TestData')),
        timeout=2
    ), "Timeout: Topic not found via type filter."

    # Test general existence
    assert _wait_for(
        lambda: any(True for _ in session_b.list_topics()),
        timeout=2
    ), "Timeout: No topics discovered at all."


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
    obj = WithContainers("test/containers", synq_authoritive=True,
                         items=EventedList([1, 2, 3]),
                         mapping=EventedDict({"a": 1}))
    remote = WithContainers(synq_session=session_b, synq_topic=obj.synq_absolute_path)

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

    session_b = Session()

    obj = WithBytes("test/bytes", synq_authoritive=True, data=b"hello\x00world")
    remote = WithBytes(synq_session=session_b, synq_topic=obj.synq_absolute_path)

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

    session_b = Session()

    obj = RehydrateExample("test/rehydrate", synq_authoritive=True, value=100, text="initial")
    # Simulate a late joiner that only wants the current state without subscribing
    snapshot = RehydrateExample(synq_session=session_b, synq_topic=obj.synq_absolute_path,
                                synq_receive=False, synq_publish=False)
    assert snapshot.value == 100
    assert snapshot.text == "initial"

    # The snapshot is a plain object, not linked for updates
    obj.value = 200
    # Wait a moment to ensure no accidental propagation
    time.sleep(0.1)
    # snapshot should stay unchanged
    assert snapshot.value == 100


def test_large_bytes_sync():
    """
    Sync a 4 MiB bytes field to verify that large binary payloads are handled correctly.
    """
    @dataclass
    class LargeBytes(SyncableObject):
        data: bytes = b""
        skip_rehydrate = {"data",}

    session_b = Session()

    # 10 MiB of data
    size = 10 * 1024 * 1024
    large_data = b"x" * size

    obj = LargeBytes("test/large_bytes", synq_authoritive=True)
    remote = LargeBytes(synq_session=session_b, synq_topic=obj.synq_absolute_path)

    # Update with different pattern
    new_data = b"y" * size
    obj.data = new_data
    assert _wait_for(lambda: remote.data == new_data, timeout=5.0), "Timeout waiting for large bytes update"
