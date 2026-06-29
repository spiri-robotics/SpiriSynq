"""Tests for SpiriSynq/syncable_objects.py covering previously uncovered code paths."""

import time
from dataclasses import dataclass
import pytest
import zenoh

from SpiriSynq.syncable_objects import (
    SyncableObject,
    SubSyncableDataclass,
    WeakMethodProxy,
    _unwrap_dataclass_types,
    _collect_valid_sync_paths,
)
from SpiriSynq.session import Session, current_session
from conftest import zenoh_test_config


def _wait_for(predicate, timeout=1.0, interval=0.01):
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture(autouse=True)
def close_test_sessions():
    from SpiriSynq.shutdown import _live_sessions

    before = set(_live_sessions.keys())
    yield
    for sid, session in list(_live_sessions.items()):
        if sid not in before:
            session.close()


class _FailUndeclare:
    """Stub that always raises on undeclare() to exercise exception-swallowing paths."""

    def undeclare(self):
        raise RuntimeError("simulated undeclare failure")


# ── Pure unit tests (no zenoh) ───────────────────────────────────────────────


def test_unwrap_dataclass_types_bare_dataclass():
    """Line 45: bare dataclass annotation (not Union-wrapped) returns [annotation]."""

    @dataclass
    class Foo:
        x: int = 0

    assert _unwrap_dataclass_types(Foo) == [Foo]


def test_unwrap_dataclass_types_non_dataclass_returns_empty():
    assert _unwrap_dataclass_types(int) == []


def test_collect_valid_sync_paths_cycle_detection():
    """Line 64: passing cls in _visited returns empty set, breaking infinite recursion."""

    @dataclass
    class Node:
        value: int = 0

    result = _collect_valid_sync_paths(Node, set(), _visited=frozenset({Node}))
    assert result == set()


def test_weakmethodproxy_equality_between_proxies():
    """Lines 192-194: two proxies for the same bound method compare equal."""

    class T:
        def method(self):
            pass

    t = T()
    assert WeakMethodProxy(t.method) == WeakMethodProxy(t.method)


def test_weakmethodproxy_equality_with_non_proxy_returns_not_implemented():
    class T:
        def method(self):
            pass

    t = T()
    assert WeakMethodProxy(t.method).__eq__("not a proxy") is NotImplemented


def test_weakmethodproxy_hash():
    """Line 197: proxies are hashable and usable in sets/dicts."""

    class T:
        def method(self):
            pass

    t = T()
    proxy = WeakMethodProxy(t.method)
    assert proxy in {proxy}


def test_resolve_sync_type_unknown_segment():
    """Line 642: segment not in dataclass fields → None."""

    @dataclass
    class Obj(SyncableObject):
        speed: float = 0.0

    assert Obj._resolve_sync_type_uncached("nonexistent") is None


def test_resolve_sync_type_primitive_at_mid_path():
    """Line 664: primitive type at an intermediate segment → can't traverse further → None."""

    @dataclass
    class Obj(SyncableObject):
        speed: float = 0.0

    assert Obj._resolve_sync_type_uncached("speed/subfield") is None


def test_resolve_sync_type_nested_dataclass_traversal():
    """Line 667: current_cls = dc_types[0] when stepping into a nested dataclass."""

    @dataclass
    class Inner:
        value: int = 0

    @dataclass
    class Obj(SyncableObject):
        nested: Inner | None = None

    result = Obj._resolve_sync_type_uncached("nested/value")
    assert result is not None
    assert int in result


def test_valid_sync_type_invalid_path_returns_false():
    """Line 674: inner_types is None for an invalid path → returns False."""

    @dataclass
    class Obj(SyncableObject):
        speed: float = 0.0

    assert Obj.valid_sync_type("does_not_exist", 42) is False


# ── Integration tests (with zenoh) ──────────────────────────────────────────


def test_sync_without_session_raises():
    """Line 259: sync() raises when synq_session is None."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    obj = Obj("test/so_no_session", synq_session=None, synq_auto_start=False)
    with pytest.raises(Exception, match="No Session"):
        obj.sync()


def test_synq_publish_false_suppresses_outgoing_updates():
    """Line 312: synq_publish=False prevents any zenoh put on field change."""

    @dataclass
    class Obj(SyncableObject):
        value: float = 0.0

    session_a = Session(config=zenoh_test_config())
    obj = Obj(
        "test/so_no_publish",
        synq_authoritive=True,
        synq_publish=False,
        synq_session=session_a,
    )
    session_b = Session(config=zenoh_test_config())

    received = []
    sub = session_b.zenoh_session.declare_subscriber(
        f"{obj.synq_absolute_path}/**", lambda s: received.append(s)
    )
    obj.value = 99.9
    time.sleep(0.05)
    sub.undeclare()

    assert received == [], "No messages expected with synq_publish=False"


def test_sync_dumps_returns_yaml_string():
    """Lines 700-701: sync_dumps() serialises current syncable state to a YAML string."""

    @dataclass
    class Obj(SyncableObject):
        speed: float = 0.0
        name: str = ""

    session_a = Session(config=zenoh_test_config())
    obj = Obj(
        "test/so_sync_dumps",
        synq_authoritive=True,
        speed=3.14,
        name="hello",
        synq_session=session_a,
    )
    result = obj.sync_dumps()
    assert isinstance(result, str)
    assert "3.14" in result
    assert "hello" in result


def test_close_idempotent():
    """Lines 711-712: calling close() twice swallows the double-disconnect error."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_close_idem", synq_authoritive=True, synq_session=session_a)
    obj.close()
    obj.close()  # must not raise


def test_close_swallows_subscriber_undeclare_error():
    """Lines 718-719: subscriber.undeclare() errors are caught and ignored."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_close_sub", synq_authoritive=True, synq_session=session_a)
    real_sub = obj.synq_subscriber
    obj.synq_subscriber = _FailUndeclare()  # type: ignore[assignment]
    assert real_sub is not None
    real_sub.undeclare()
    obj.close()  # must not raise


def test_close_swallows_publisher_undeclare_error():
    """Lines 726-727: publisher.undeclare() errors are caught and ignored."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_close_pub", synq_authoritive=True, synq_session=session_a)
    real_pub = obj.synq_publisher
    obj.synq_publisher = _FailUndeclare()  # type: ignore[assignment]
    assert real_pub is not None
    real_pub.undeclare()
    obj.close()  # must not raise


def test_close_swallows_queryable_undeclare_error():
    """Lines 733-734: queryable.undeclare() errors in _synq_callbacks are caught."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_close_q", synq_authoritive=True, synq_session=session_a)
    for q in list(obj._synq_callbacks.values()):
        q.undeclare()
    obj._synq_callbacks = {"fake": _FailUndeclare()}
    obj.close()  # must not raise


def test_del_handles_missing_synq_topic():
    """Lines 742-743: __del__ catches AttributeError from synq_absolute_path."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    obj = Obj("test/so_del_attr", synq_session=None, synq_auto_start=False)
    del obj.synq_topic  # makes synq_absolute_path raise AttributeError
    obj.__del__()  # must not propagate


def test_signal_unknown_path_emitted():
    """Lines 408-412: update at an unrecognised sub-path fires synq_signal_unknown_path."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_unknown_path", synq_authoritive=True, synq_session=session_a)
    session_b = Session(config=zenoh_test_config())
    mirror = Obj.from_topic(obj.synq_absolute_path, session=session_b)

    unknown: list[str] = []
    mirror.synq_signal_unknown_path.connect(lambda path, *_: unknown.append(path))

    _wait_for(lambda: obj.synq_publisher.matching_status.matching)  # type: ignore[union-attr]
    # Publish from the default session without source_info so the ZID filter
    # on mirror (session_b) does not suppress it.
    current_session.get().zenoh_session.put(
        f"{obj.synq_absolute_path}/nonexistent_field",
        "irrelevant",
        encoding=zenoh.Encoding.APPLICATION_YAML,
    )
    assert _wait_for(lambda: len(unknown) > 0), "synq_signal_unknown_path never fired"
    assert unknown[0] == "nonexistent_field"


def test_signal_type_mismatch_emitted_and_value_unchanged():
    """Lines 425-429: wrong-type update fires synq_signal_type_mismatch and is dropped."""

    @dataclass
    class Obj(SyncableObject):
        value: float = 0.0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_type_mismatch", synq_authoritive=True, synq_session=session_a)
    session_b = Session(config=zenoh_test_config())
    mirror = Obj.from_topic(obj.synq_absolute_path, session=session_b)

    mismatches: list = []
    mirror.synq_signal_type_mismatch.connect(
        lambda path, val: mismatches.append((path, val))
    )

    _wait_for(lambda: obj.synq_publisher.matching_status.matching)  # type: ignore[union-attr]
    # YAML string is not a float — publish without source_info so it reaches mirror
    current_session.get().zenoh_session.put(
        f"{obj.synq_absolute_path}/value",
        '"not_a_float"',
        encoding=zenoh.Encoding.APPLICATION_YAML,
    )
    assert _wait_for(lambda: len(mismatches) > 0), "synq_signal_type_mismatch never fired"
    assert mismatches[0][0] == "value"
    assert mirror.value == 0.0, "Field must not be updated on type mismatch"


def test_binary_payload_rejected_for_non_bytes_field():
    """
    Publishing a ZENOH_BYTES payload to a non-bytes field should be rejected,
    not silently accepted as raw bytes.
    """

    @dataclass
    class InnerType:
        x: int = 0

    @dataclass
    class Obj(SyncableObject):
        value: InnerType | None = None

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_binary_reject", synq_authoritive=True, synq_session=session_a)
    session_b = Session(config=zenoh_test_config())
    mirror = Obj.from_topic(obj.synq_absolute_path, session=session_b)

    mismatches: list = []
    mirror.synq_signal_type_mismatch.connect(
        lambda path, val: mismatches.append((path, val))
    )

    _wait_for(lambda: obj.synq_publisher.matching_status.matching)  # type: ignore[union-attr]
    # Publish raw binary payload (ZENOH_BYTES) to a non-bytes field
    current_session.get().zenoh_session.put(
        f"{obj.synq_absolute_path}/value",
        b"raw_binary_garbage",
        encoding=zenoh.Encoding.ZENOH_BYTES,
    )
    assert _wait_for(
        lambda: len(mismatches) > 0
    ), "synq_signal_type_mismatch should fire for binary payload on non-bytes field"
    assert mismatches[0][0] == "value", "Mismatch path should be 'value'"
    assert mirror.value is None, "Field must not be updated on type mismatch"


def test_signal_missing_parent_emitted():
    """Lines 438-450: nested update with a None parent fires synq_signal_missing_parent."""

    @dataclass
    class Inner(SubSyncableDataclass):
        value: int = 0

    @dataclass
    class Outer(SyncableObject):
        inner: Inner | None = None

    session_a = Session(config=zenoh_test_config())
    obj = Outer(
        "test/so_missing_parent",
        synq_authoritive=True,
        synq_session=session_a,
        synq_auto_rehydrate_on_missing_parent_timeout=-1,
    )
    session_b = Session(config=zenoh_test_config())
    mirror = Outer.from_topic(obj.synq_absolute_path, session=session_b)
    mirror.synq_auto_rehydrate_on_missing_parent_timeout = -1
    assert mirror.inner is None

    missing: list[str] = []
    mirror.synq_signal_missing_parent.connect(lambda path, *_: missing.append(path))

    _wait_for(lambda: obj.synq_publisher.matching_status.matching)  # type: ignore[union-attr]
    # Publish inner/value while inner is None — publish without source_info
    current_session.get().zenoh_session.put(
        f"{obj.synq_absolute_path}/inner/value",
        "42",
        encoding=zenoh.Encoding.APPLICATION_YAML,
    )
    assert _wait_for(lambda: len(missing) > 0), "synq_signal_missing_parent never fired"
    assert missing[0] == "inner/value"
    assert mirror.inner is None, "inner must remain None after missing-parent update"


def test_weakmethodproxy_dead_ref_raises():
    """Line 188: calling a proxy after the referenced object is GC'd raises ReferenceError."""
    import gc

    class T:
        def method(self):
            pass

    t = T()
    proxy = WeakMethodProxy(t.method)
    del t
    gc.collect()
    with pytest.raises(ReferenceError):
        proxy()


def test_from_topic_without_session_uses_current():
    """Line 496: from_topic() with no session= falls back to current_session.get()."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj(
        "test/so_from_topic_noarg",
        synq_authoritive=True,
        value=7,
        synq_session=session_a,
    )

    session_b = Session(config=zenoh_test_config())
    with session_b.as_default():
        # No session= argument → takes the `if not session:` branch (line 496)
        mirror = Obj.from_topic(obj.synq_absolute_path)

    assert isinstance(mirror, Obj)
    assert mirror.value == 7


def test_close_swallows_events_disconnect_error():
    """Lines 711-712: errors from events.disconnect() are caught and ignored."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_close_evt", synq_authoritive=True, synq_session=session_a)

    def raise_error(*_: object) -> None:
        raise RuntimeError("simulated disconnect failure")

    obj.events.disconnect = raise_error  # type: ignore[attr-defined]
    obj.close()  # must not propagate


def test_signal_missing_parent_triggers_auto_rehydrate():
    """Lines 443-447: positive timeout spawns an auto-rehydrate thread on missing-parent."""

    @dataclass
    class Inner(SubSyncableDataclass):
        value: int = 0

    @dataclass
    class Outer(SyncableObject):
        inner: Inner | None = None

    session_a = Session(config=zenoh_test_config())
    obj = Outer("test/so_auto_rehydrate", synq_authoritive=True, synq_session=session_a)
    session_b = Session(config=zenoh_test_config())
    mirror = Outer.from_topic(obj.synq_absolute_path, session=session_b)
    # Leave synq_auto_rehydrate_on_missing_parent_timeout at default (5.0s)
    # so the rehydrate thread is spawned when the missing-parent signal fires.
    assert mirror.inner is None

    missing: list[str] = []
    mirror.synq_signal_missing_parent.connect(lambda path, *_: missing.append(path))

    _wait_for(lambda: obj.synq_publisher.matching_status.matching)  # type: ignore[union-attr]
    current_session.get().zenoh_session.put(
        f"{obj.synq_absolute_path}/inner/value",
        "42",
        encoding=zenoh.Encoding.APPLICATION_YAML,
    )
    assert _wait_for(lambda: len(missing) > 0), "synq_signal_missing_parent never fired"
    assert mirror.inner is None  # rehydrate confirms inner=None; state is unchanged


def test_sr_rehydrate_no_diff():
    """Line 496: sr_rehydrate() exits early without applying a delta when state matches."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj(
        "test/so_rehydrate_nodiff",
        synq_authoritive=True,
        value=42,
        synq_session=session_a,
    )
    session_b = Session(config=zenoh_test_config())
    mirror = Obj.from_topic(obj.synq_absolute_path, session=session_b)
    assert mirror.value == 42

    # State already matches the authoritative object; the no-diff path (line 496) executes
    mirror.sr_rehydrate()
    assert mirror.value == 42


# ── Tombstone tests ──────────────────────────────────────────────────────────


def test_tombstone_publisher_created_for_authoritative():
    """Authoritative objects get a reliable tombstone publisher in sync()."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_tombstone_pub", synq_authoritive=True, synq_session=session_a)
    assert hasattr(obj, "_synq_tombstone_publisher")
    assert obj._synq_tombstone_publisher is not None
    obj.close()


def test_tombstone_publisher_not_created_for_non_authoritative():
    """Non-authoritative mirrors must not get a tombstone publisher."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_tombstone_noauth", synq_authoritive=True, synq_session=session_a)
    session_b = Session(config=zenoh_test_config())
    mirror = Obj.from_topic(obj.synq_absolute_path, session=session_b)
    assert not getattr(mirror, "_synq_tombstone_publisher", None)
    obj.close()


def test_authoritative_close_sets_is_deleted():
    """close() on an authoritative object sets synq_is_deleted=True."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_tombstone_selfdelete", synq_authoritive=True, synq_session=session_a)
    assert not obj.synq_is_deleted
    obj.close()
    assert obj.synq_is_deleted


def test_non_authoritative_close_does_not_set_is_deleted():
    """close() on a mirror must not set synq_is_deleted=True."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_tombstone_mirror_close", synq_authoritive=True, synq_session=session_a)
    session_b = Session(config=zenoh_test_config())
    mirror = Obj.from_topic(obj.synq_absolute_path, session=session_b)
    mirror.close()
    assert not mirror.synq_is_deleted
    obj.close()


def test_tombstone_received_sets_is_deleted_and_emits_signal():
    """Mirror receives the DELETE sample, sets synq_is_deleted, and fires synq_signal_tombstone."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_tombstone_receive", synq_authoritive=True, synq_session=session_a)
    session_b = Session(config=zenoh_test_config())
    mirror = Obj.from_topic(obj.synq_absolute_path, session=session_b)

    tombstones: list = []
    mirror.synq_signal_tombstone.connect(lambda: tombstones.append(True))

    obj.close()

    assert _wait_for(lambda: len(tombstones) > 0), "synq_signal_tombstone never fired on mirror"
    assert mirror.synq_is_deleted


def test_tombstone_not_echoed_back_to_sender():
    """Authoritative object's own DELETE sample must not trigger its own signal/is_deleted flag."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_tombstone_noecho", synq_authoritive=True, synq_session=session_a)

    self_tombstones: list = []
    obj.synq_signal_tombstone.connect(lambda: self_tombstones.append(True))

    obj.close()

    time.sleep(0.05)
    assert self_tombstones == [], "Authoritative object must not receive its own tombstone"


def test_close_swallows_tombstone_publisher_errors():
    """close() must not raise if the tombstone publisher's delete() or undeclare() fails."""

    @dataclass
    class Obj(SyncableObject):
        value: int = 0

    session_a = Session(config=zenoh_test_config())
    obj = Obj("test/so_tombstone_err", synq_authoritive=True, synq_session=session_a)

    class _FailAll:
        def delete(self, **_):
            raise RuntimeError("tombstone delete failed")

        def undeclare(self):
            raise RuntimeError("tombstone undeclare failed")

    obj._synq_tombstone_publisher = _FailAll()  # type: ignore[assignment]
    obj.close()  # must not raise
