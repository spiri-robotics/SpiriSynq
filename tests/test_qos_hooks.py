"""Tests for per-field QoS (<field>_qos) and custom publish/receive hooks."""

import time
from dataclasses import dataclass
from typing import ClassVar, Iterator

import pytest
import zenoh

from SpiriSynq.qos import SynqQoS
from SpiriSynq.schema import get_schema
from SpiriSynq.session import Session
from SpiriSynq.syncable_objects import (
    SKIP,
    PutArgs,
    SubSyncableDataclass,
    SyncableObject,
)
from conftest import zenoh_test_config


def _wait_for(predicate, timeout=3.0, interval=0.01):
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


def _connect(auth, mirror):
    """Bump auth.ready until the mirror sees it, so later publishes have a route."""

    def bump():
        auth.ready += 1
        return _wait_for(lambda: mirror.ready == auth.ready, timeout=0.1)

    assert _wait_for(bump), "mirror never connected"


# ── SynqQoS ──────────────────────────────────────────────────────────────────


def test_qos_int_priority_rejected():
    with pytest.raises(TypeError, match="zenoh.Priority"):
        SynqQoS(priority=4)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="zenoh.CongestionControl"):
        SynqQoS(congestion_control=1)  # type: ignore[arg-type]


def test_qos_hashable_and_comparable():
    a = SynqQoS(priority=zenoh.Priority.DATA_HIGH)
    assert a == SynqQoS(priority=zenoh.Priority.DATA_HIGH)
    assert a != SynqQoS()
    assert hash(a) == hash(SynqQoS(priority=zenoh.Priority.DATA_HIGH))


def test_qos_defaults_match_zenoh():
    assert SynqQoS().put_kwargs() == {
        "priority": zenoh.Priority.DATA,
        "congestion_control": zenoh.CongestionControl.DROP,
        "express": False,
    }


# ── QoS resolution ───────────────────────────────────────────────────────────


@dataclass
class Inner(SubSyncableDataclass):
    value: int = 0
    name: str = ""
    name_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.INTERACTIVE_HIGH)


@dataclass
class QosObj(SyncableObject):
    image: bytes = b""
    other: int = 0
    mode_qos: str = "fast"  # ordinary field, not a QoS declaration
    bar: Inner | None = None
    ready: int = 0
    image_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.DATA_HIGH)
    bar_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.DATA_LOW)


def test_qos_resolution_class_instance_and_nested():
    obj = QosObj("test/qos_resolve", bar=Inner())
    assert obj.synq_qos_for("image").priority == zenoh.Priority.DATA_HIGH
    assert obj.synq_qos_for("other") is None
    # bar/value has no value_qos on Inner -> falls back to bar_qos
    assert obj.synq_qos_for("bar/value").priority == zenoh.Priority.DATA_LOW
    # bar/name has its own name_qos on Inner
    assert obj.synq_qos_for("bar/name").priority == zenoh.Priority.INTERACTIVE_HIGH

    obj.image_qos = SynqQoS(priority=zenoh.Priority.REAL_TIME)
    assert obj.synq_qos_for("image").priority == zenoh.Priority.REAL_TIME
    assert QosObj.image_qos.priority == zenoh.Priority.DATA_HIGH


def test_qos_non_synqqos_field_is_ordinary():
    obj = QosObj("test/qos_ordinary")
    assert "mode_qos" in QosObj.valid_sync_paths()
    assert obj.synq_qos_for("mode") is None


def test_qos_classvar_not_synced_or_in_schema():
    paths = QosObj.valid_sync_paths()
    assert "image_qos" not in paths and "bar_qos" not in paths
    assert "image_qos" not in str(get_schema(QosObj))


def test_qos_instance_assignment_emits_no_event():
    obj = QosObj("test/qos_noevent")
    events = []
    obj.events.connect(lambda info: events.append(info))
    obj.image_qos = SynqQoS(priority=zenoh.Priority.INTERACTIVE_HIGH)
    assert events == []


def test_qos_applied_on_wire():
    session_a = Session(config=zenoh_test_config())
    session_b = Session(config=zenoh_test_config())
    auth = QosObj("test/qos_wire", synq_authoritive=True, synq_session=session_a)
    mirror = QosObj.from_topic(auth.synq_absolute_path, session=session_b)
    _connect(auth, mirror)

    priorities: dict[str, zenoh.Priority] = {}
    sub = session_b.zenoh_session.declare_subscriber(
        f"{auth.synq_absolute_path}/**",
        lambda s: priorities.__setitem__(str(s.key_expr).rsplit("/", 1)[-1], s.priority),
    )
    try:

        def publish():
            auth.image = auth.image + b"x"
            auth.other += 1
            return "image" in priorities and "other" in priorities

        assert _wait_for(publish)
        assert priorities["image"] == zenoh.Priority.DATA_HIGH
        assert priorities["other"] == zenoh.Priority.DATA

        auth.image_qos = SynqQoS(priority=zenoh.Priority.BACKGROUND)
        priorities.clear()
        assert _wait_for(lambda: publish() and priorities["image"] == zenoh.Priority.BACKGROUND)
    finally:
        sub.undeclare()


# ── Publish / receive hooks ──────────────────────────────────────────────────


@dataclass
class Chunked(SyncableObject):
    """Publishes ``data`` as three chunks; the receiver reassembles them."""

    data: bytes = b""
    ready: int = 0
    data_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.DATA_LOW)

    def __post_init__(self):
        self._chunks: dict[int, bytes] = {}
        self.priorities: list = []
        super().__post_init__()

    def data_publish(self, value) -> Iterator[PutArgs]:
        size = -(-len(value) // 3) or 1
        for i in range(3):
            args: PutArgs = {"payload": bytes([i]) + value[i * size : (i + 1) * size]}
            if i == 0:
                # first chunk overrides the field QoS, the rest use it
                args["priority"] = zenoh.Priority.INTERACTIVE_HIGH
            yield args

    def data_receive(self, value, sample):
        self.priorities.append(sample.priority)
        self._chunks[value[0]] = value[1:]
        if len(self._chunks) < 3:
            return SKIP
        whole = b"".join(self._chunks[i] for i in range(3))
        self._chunks.clear()
        return whole


@dataclass
class Plain(SyncableObject):
    data: bytes = b""
    ready: int = 0


def test_publish_and_receive_hooks_round_trip():
    session_a = Session(config=zenoh_test_config())
    session_b = Session(config=zenoh_test_config())
    auth = Chunked("test/hook_rt", synq_authoritive=True, synq_session=session_a)
    mirror = Chunked.from_topic(auth.synq_absolute_path, session=session_b)
    _connect(auth, mirror)

    seen = []
    mirror.events.data.connect(lambda v: seen.append(v))
    auth.data = b"abcdefghi"
    assert _wait_for(lambda: mirror.data == b"abcdefghi")
    # intermediate chunks returned SKIP, so the field changed exactly once
    assert seen == [b"abcdefghi"]
    assert sorted(mirror.priorities, key=int) == [
        zenoh.Priority.INTERACTIVE_HIGH,
        zenoh.Priority.DATA_LOW,
        zenoh.Priority.DATA_LOW,
    ]


def test_receive_hook_skip_leaves_field_unchanged():
    session_a = Session(config=zenoh_test_config())
    session_b = Session(config=zenoh_test_config())
    auth = Plain("test/hook_skip", synq_authoritive=True, synq_session=session_a)

    @dataclass
    class Rejecting(SyncableObject):
        data: bytes = b"orig"
        ready: int = 0

        def data_receive(self, value, sample):
            return SKIP

    mirror = Rejecting(auth.synq_topic, synq_base_topic=auth.synq_base_topic, synq_session=session_b)
    _connect(auth, mirror)
    auth.data = b"new"
    assert not _wait_for(lambda: mirror.data != b"orig", timeout=0.3)


def test_receive_hook_return_value_is_type_checked():
    session_a = Session(config=zenoh_test_config())
    session_b = Session(config=zenoh_test_config())
    auth = Plain("test/hook_typecheck", synq_authoritive=True, synq_session=session_a)

    @dataclass
    class Wrong(SyncableObject):
        data: bytes = b""
        ready: int = 0

        def data_receive(self, value, sample):
            return "not bytes"

    mirror = Wrong(auth.synq_topic, synq_base_topic=auth.synq_base_topic, synq_session=session_b)
    mismatches = []
    mirror.synq_signal_type_mismatch.connect(lambda path, val: mismatches.append(val))
    _connect(auth, mirror)

    def publish():
        auth.data = auth.data + b"x"
        return bool(mismatches)

    assert _wait_for(publish)
    assert mismatches[0] == "not bytes"
    assert mirror.data == b""


def test_hookless_peer_sees_raw_chunks():
    session_a = Session(config=zenoh_test_config())
    session_b = Session(config=zenoh_test_config())
    auth = Chunked("test/hook_hookless", synq_authoritive=True, synq_session=session_a)
    mirror = Plain(auth.synq_topic, synq_base_topic=auth.synq_base_topic, synq_session=session_b)
    _connect(auth, mirror)

    received = []
    mirror.events.data.connect(lambda v: received.append(v))
    auth.data = b"abcdefghi"
    assert _wait_for(lambda: len(received) == 3)
    assert sorted(received) == [b"\x00abc", b"\x01def", b"\x02ghi"]


def test_publish_hook_is_testable_without_zenoh():
    obj = Chunked("test/hook_offline", synq_auto_start=False)
    assert list(obj.data_publish(b"abcdef")) == [
        {"payload": b"\x00ab", "priority": zenoh.Priority.INTERACTIVE_HIGH},
        {"payload": b"\x01cd"},
        {"payload": b"\x02ef"},
    ]


def test_publish_hook_bare_bytes_and_empty():
    session_a = Session(config=zenoh_test_config())
    session_b = Session(config=zenoh_test_config())

    @dataclass
    class Filtered(SyncableObject):
        data: bytes = b""
        ready: int = 0

        def data_publish(self, value):
            if value.startswith(b"keep"):
                yield value  # bare bytes shorthand
            # anything else: nothing yielded, nothing sent

    auth = Filtered("test/hook_bare", synq_authoritive=True, synq_session=session_a)
    mirror = Plain(auth.synq_topic, synq_base_topic=auth.synq_base_topic, synq_session=session_b)
    _connect(auth, mirror)

    received = []
    mirror.events.data.connect(lambda v: received.append(v))
    auth.data = b"drop me"
    auth.data = b"keep me"
    assert _wait_for(lambda: received == [b"keep me"])
    assert not _wait_for(lambda: len(received) > 1, timeout=0.2)


def test_publish_hook_bad_yield_raises():
    @dataclass
    class Bad(SyncableObject):
        data: int = 0

        def data_publish(self, value):
            yield value  # an int is not a sample

    obj = Bad("test/hook_bad", synq_authoritive=True)
    with pytest.raises(TypeError, match="expected a PutArgs dict"):
        obj._synq_publish_path("data", 5)


@pytest.mark.parametrize("key", ["key_expr", "source_info"])
def test_publish_hook_reserved_keys_rejected(key):
    @dataclass
    class Hijack(SyncableObject):
        data: int = 0

        def data_publish(self, value):
            yield {"payload": b"x", key: None}

    obj = Hijack("test/hook_reserved", synq_authoritive=True)
    with pytest.raises(ValueError, match=key):
        obj._synq_publish_path("data", 5)


def test_publish_hook_unknown_key_fails_in_zenoh():
    @dataclass
    class Typo(SyncableObject):
        data: int = 0

        def data_publish(self, value):
            yield {"payload": b"x", "priorty": 4}

    obj = Typo("test/hook_typo", synq_authoritive=True)
    with pytest.raises(TypeError, match="priorty"):
        obj._synq_publish_path("data", 5)


def test_field_named_like_hook_is_not_a_hook():
    @dataclass
    class Obj(SyncableObject):
        data: int = 0
        data_publish: bool = True

    obj = Obj("test/hook_field_name")
    assert obj._synq_field_hook("data", "publish") is None


# ── Nested paths ─────────────────────────────────────────────────────────────


@dataclass
class Leaf(SubSyncableDataclass):
    x: int = 0
    y: int = 0
    x_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.REAL_TIME)


@dataclass
class Mid(SubSyncableDataclass):
    leaf: Leaf | None = None
    z: int = 0
    leaf_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.INTERACTIVE_HIGH)


@dataclass
class Deep(SyncableObject):
    mid: Mid | None = None
    mid_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.BACKGROUND)


def test_qos_resolution_three_levels():
    obj = Deep("test/qos_deep", mid=Mid(leaf=Leaf()))
    assert obj.synq_qos_for("mid").priority == zenoh.Priority.BACKGROUND
    assert obj.synq_qos_for("mid/z").priority == zenoh.Priority.BACKGROUND
    assert obj.synq_qos_for("mid/leaf").priority == zenoh.Priority.INTERACTIVE_HIGH
    assert obj.synq_qos_for("mid/leaf/x").priority == zenoh.Priority.REAL_TIME
    assert obj.synq_qos_for("mid/leaf/y").priority == zenoh.Priority.INTERACTIVE_HIGH

    # instance override on a sub-object
    obj.mid.leaf.y_qos = SynqQoS(priority=zenoh.Priority.DATA_HIGH)
    assert obj.synq_qos_for("mid/leaf/y").priority == zenoh.Priority.DATA_HIGH

    # a None parent stops the walk; the nearest live ancestor's QoS applies
    obj.mid.leaf = None
    assert obj.synq_qos_for("mid/leaf/x").priority == zenoh.Priority.INTERACTIVE_HIGH


@dataclass
class HookInner(SubSyncableDataclass):
    value: int = 0

    def value_publish(self, value):
        yield f"v={value}".encode()

    def value_receive(self, value, sample):
        self.received = value
        return int(value.removeprefix(b"v="))


@dataclass(frozen=True)
class FrozenInner:
    value: int = 0

    def value_publish(self, value):
        yield b""


@dataclass
class HookOuter(SyncableObject):
    bar: HookInner | None = None
    frozen: FrozenInner | None = None
    ready: int = 0

    def value_publish(self, value):  # not bar/value's hook: wrong owner
        raise AssertionError("unreachable")

    def bar_publish(self, value):  # whole-bar replacement only, no ancestor fallback
        raise AssertionError("unreachable")


def test_nested_hook_lookup():
    obj = HookOuter("test/hook_nested_lookup", bar=HookInner(), frozen=FrozenInner())
    hook = obj._synq_field_hook("bar/value", "publish")
    assert hook.__self__ is obj.bar and hook.__name__ == "value_publish"
    assert obj._synq_field_hook("frozen/value", "publish").__self__ is obj.frozen
    assert obj._synq_field_hook("bar/value", "receive").__self__ is obj.bar

    obj.bar = None
    assert obj._synq_field_hook("bar/value", "publish") is None


def test_nested_hooks_round_trip():
    session_a = Session(config=zenoh_test_config())
    session_b = Session(config=zenoh_test_config())
    auth = HookOuter(
        "test/hook_nested_rt", bar=HookInner(), synq_authoritive=True, synq_session=session_a
    )
    mirror = HookOuter.from_topic(auth.synq_absolute_path, session=session_b)
    assert mirror.bar is not None
    _connect(auth, mirror)

    auth.bar.value = 7
    assert _wait_for(lambda: mirror.bar.value == 7)
    assert mirror.bar.received == b"v=7"
