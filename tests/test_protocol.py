"""Wire-level checks for the claims in docs/protocol.md.

These talk to an authoritative object with raw zenoh calls only, the way a
non-Python implementation would, so a change to the wire format fails here
before the spec silently goes stale.
"""
import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Generator

import pytest
import yaml
import zenoh

from SpiriSynq.remote_callables import GENERATOR_DONE_ENCODING, remote_method
from SpiriSynq.syncable_objects import SubSyncableDataclass, SyncableObject


def _wait_for(predicate, timeout=2.0, interval=0.01):
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


@dataclass
class ProtoGimbal(SubSyncableDataclass):
    """A gimbal."""
    pitch: float = 0.0


@dataclass
class ProtoProbe(SyncableObject):
    """Probe docstring."""
    speed: float = field(default=0.0, metadata={"help": "metres per second"})
    maybe: int | None = None
    gimbal: ProtoGimbal | None = None
    blob: bytes | None = None

    @remote_method()
    def add(self, a: int, b: int = 1) -> int:
        """Add numbers."""
        return a + b

    @remote_method()
    def count(self, n: int) -> Generator[int, None, str]:
        for i in range(n):
            yield i
        return "done"

    @remote_method()
    def boom(self) -> None:
        raise ValueError("nope")

    @remote_method()
    def echo(self, v: str) -> str:
        return v


def _probe():
    return ProtoProbe(synq_topic=f"proto/{uuid.uuid4().hex}", synq_authoritive=True)


def _get(z, selector, timeout=2.0):
    """All replies to *selector* as (ok, key, encoding, payload) tuples."""
    out = []
    for r in z.get(selector, consolidation=zenoh.ConsolidationMode.NONE, timeout=timeout):
        if r.ok:
            out.append((True, str(r.ok.key_expr), str(r.ok.encoding), r.ok.payload.to_string()))
        else:
            out.append((False, None, str(r.err.encoding), r.err.payload.to_string()))
    return out


def test_metadata_served_bare_and_per_tag(zenoh_current_session):
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    p = obj.synq_absolute_path
    for key in (f"{p}/sr_metadata", f"{p}/sr_metadata/ProtoProbe", f"{p}/sr_metadata/SyncableObject"):
        replies = _get(z, key)
        assert len(replies) == 1, key
        ok, reply_key, encoding, payload = replies[0]
        assert ok and reply_key == key and encoding == "application/yaml"
        meta = yaml.safe_load(payload)
        assert meta["topic"] == p
        assert meta["classes"] == ["!ProtoProbe", "!SyncableObject"]
        assert meta["authoritive_node"] == str(z.zid())


def test_discovery_query_matches_bare_metadata_key(zenoh_current_session):
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    keys = [r[1] for r in _get(z, "**/sr_metadata")]
    assert f"{obj.synq_absolute_path}/sr_metadata" in keys


def test_rehydrate_is_tagged_yaml_with_binary(zenoh_current_session):
    obj = _probe()
    obj.blob = b"\x00\x01"
    z = zenoh_current_session.zenoh_session
    (ok, _, encoding, payload), = _get(z, f"{obj.synq_absolute_path}/sr_rehydrate")
    assert ok and encoding == "application/yaml"
    assert payload.startswith("!ProtoProbe\n")
    assert "blob: !!binary" in payload
    assert "synq_topic:" in payload and "synq_base_topic:" in payload


def test_rpc_parameters_are_semicolon_separated_yaml(zenoh_current_session):
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    (ok, key, encoding, payload), = _get(z, f"{obj.synq_absolute_path}/add?a=2;b=40")
    assert ok and key == f"{obj.synq_absolute_path}/add" and encoding == "application/yaml"
    assert yaml.safe_load(payload) == 42
    # '&' is not a separator: the whole tail becomes the value of `a`.
    (ok, _, _, _), = _get(z, f"{obj.synq_absolute_path}/add?a=2&b=40")
    assert not ok


def test_rpc_error_is_reply_err_string(zenoh_current_session):
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    (ok, _, _, payload), = _get(z, f"{obj.synq_absolute_path}/boom")
    assert not ok
    assert payload == "RPC error 'nope'"


def test_generator_replies_then_done_encoding(zenoh_current_session):
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    replies = _get(z, f"{obj.synq_absolute_path}/count?n=3")
    assert [r[2] for r in replies] == ["application/yaml"] * 3 + [str(GENERATOR_DONE_ENCODING)]
    assert [yaml.safe_load(r[3]) for r in replies] == [0, 1, 2, "done"]


def test_field_puts_carry_per_publisher_source_info(zenoh_current_session):
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    p = obj.synq_absolute_path
    seen = []
    sub = z.declare_subscriber(f"{p}/**", seen.append)
    try:
        obj.speed = 2.5
        obj.gimbal = ProtoGimbal()
        obj.gimbal.pitch = 3.0
        obj.blob = b"\x00\x01"
        assert _wait_for(lambda: len(seen) >= 4)
    finally:
        sub.undeclare()

    by_key = {str(s.key_expr): s for s in seen}
    assert by_key[f"{p}/speed"].payload.to_string() == "2.5"
    assert str(by_key[f"{p}/speed"].encoding) == "application/yaml"
    assert by_key[f"{p}/gimbal/pitch"].payload.to_string() == "3.0"
    assert by_key[f"{p}/gimbal"].payload.to_string().startswith("!ProtoGimbal")
    assert str(by_key[f"{p}/blob"].encoding) == "zenoh/bytes"
    assert by_key[f"{p}/blob"].payload.to_bytes() == b"\x00\x01"

    pub_id = obj.synq_publisher.id
    for s in seen:
        assert str(s.source_info.source_id.zid) == str(pub_id.zid)
        assert s.source_info.source_id.eid == pub_id.eid
    assert [s.source_info.source_sn for s in seen] == list(range(len(seen)))


def test_tombstone_is_delete_on_wildcard_continuing_the_sequence(zenoh_current_session):
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    p = obj.synq_absolute_path
    pub_id = obj.synq_publisher.id
    seen = []
    sub = z.declare_subscriber(f"{p}/**", seen.append)
    try:
        obj.speed = 1.0
        assert _wait_for(lambda: len(seen) == 1)
        obj.close()
        assert _wait_for(lambda: any(s.kind == zenoh.SampleKind.DELETE for s in seen))
    finally:
        sub.undeclare()
    put, tomb = seen
    assert tomb.kind == zenoh.SampleKind.DELETE
    assert str(tomb.key_expr) == f"{p}/**"
    assert (str(tomb.source_info.source_id.zid), tomb.source_info.source_id.eid) == (
        str(pub_id.zid), pub_id.eid
    )
    assert tomb.source_info.source_sn == put.source_info.source_sn + 1


def test_type_schema_is_object_schema_per_tag(zenoh_current_session):
    """sr_type_schema/<Tag> is sr_object_schema on another key, one per MRO tag,
    like sr_metadata/<Tag>; the reply key says which object answered."""
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    p = obj.synq_absolute_path
    (_, _, _, object_schema), = _get(z, f"{p}/sr_object_schema")

    for tag in ("ProtoProbe", "SyncableObject"):
        replies = [r for r in _get(z, f"**/sr_type_schema/{tag}") if r[1].startswith(f"{p}/")]
        assert replies == [(True, f"{p}/sr_type_schema/{tag}", "application/yaml", object_schema)]


def test_type_schema_every_object_answers(zenoh_current_session):
    a, b = _probe(), _probe()
    z = zenoh_current_session.zenoh_session
    keys = {r[1] for r in _get(z, "**/sr_type_schema/ProtoProbe")}
    assert {f"{a.synq_absolute_path}/sr_type_schema/ProtoProbe",
            f"{b.synq_absolute_path}/sr_type_schema/ProtoProbe"} <= keys


def test_type_schema_not_served_by_mirrors(zenoh_current_session):
    @dataclass
    class ProtoMirrorOnly(SyncableObject):
        value: int = 0

    ProtoMirrorOnly(synq_topic=f"proto/{uuid.uuid4().hex}")
    z = zenoh_current_session.zenoh_session
    assert _get(z, "**/sr_type_schema/ProtoMirrorOnly") == []


def test_object_schema_shape(zenoh_current_session):
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    (ok, _, encoding, payload), = _get(z, f"{obj.synq_absolute_path}/sr_object_schema")
    assert ok and encoding == "application/yaml"
    schema = yaml.safe_load(payload)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["x-spirisynq-schema"] == 2
    assert schema["title"] == "ProtoProbe"
    assert schema["x-yaml-tag"] == "!ProtoProbe"
    assert schema["x-classes"] == ["!ProtoProbe", "!SyncableObject"]
    assert schema["description"] == "Probe docstring."

    props = schema["properties"]
    assert set(props) == {"speed", "maybe", "gimbal", "blob"}  # no synq_* addressing
    assert schema["required"] == sorted(props)
    assert props["speed"] == {"type": "number", "description": "metres per second", "default": 0.0}
    assert props["maybe"] == {"type": ["integer", "null"], "default": None}
    assert props["gimbal"] == {"anyOf": [{"$ref": "#/$defs/ProtoGimbal"}, {"type": "null"}], "default": None}
    assert props["blob"]["x-encoding"] == "zenoh/bytes"
    assert schema["$defs"]["ProtoGimbal"]["description"] == "A gimbal."

    rpc = schema["x-rpc-endpoints"]
    assert set(rpc) == {"add", "boom", "count", "echo", "sr_metadata", "sr_object_schema", "sr_rehydrate"}
    assert rpc["add"] == {
        "description": "Add numbers.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer", "default": 1}},
            "required": ["a"],
            "additionalProperties": False,
        },
        "returns": {"type": "integer"},
    }
    assert rpc["count"]["x-generator"] is True
    assert rpc["count"]["yields"] == {"type": "integer"}
    assert rpc["count"]["returns"] == {"type": "string"}
    assert rpc["boom"]["returns"] == {"type": "null"}


def test_every_described_endpoint_has_a_queryable(zenoh_current_session):
    """x-rpc-endpoints must list exactly what the object serves."""
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    p = obj.synq_absolute_path
    schema = yaml.safe_load(_get(z, f"{p}/sr_object_schema")[0][3])
    declared = {k.removeprefix(f"{p}/") for k in obj._synq_callbacks}
    declared = {k for k in declared if "/" not in k}  # drop sr_metadata/<Tag> aliases
    assert declared == set(schema["x-rpc-endpoints"])


# ── Known bugs (todo.yaml). Strict xfails: these start failing when fixed. ─────

@pytest.mark.xfail(strict=True, reason="todo: ';' in an RPC argument value gets no reply")
def test_rpc_argument_containing_semicolon(zenoh_current_session):
    obj = _probe()
    z = zenoh_current_session.zenoh_session
    replies = _get(z, f"{obj.synq_absolute_path}/echo?v='a;b'", timeout=0.5)
    assert replies and yaml.safe_load(replies[0][3]) == "a;b"


class ProtoColor(enum.Enum):
    RED = "red"


@dataclass
class ProtoWithEnum(SyncableObject):
    color: ProtoColor = ProtoColor.RED


@pytest.mark.xfail(strict=True, reason="todo: Enum fields can't be YAML-serialised")
def test_rehydrate_object_with_enum_field(zenoh_current_session):
    obj = ProtoWithEnum(synq_topic=f"proto/{uuid.uuid4().hex}", synq_authoritive=True)
    z = zenoh_current_session.zenoh_session
    (ok, _, _, payload), = _get(z, f"{obj.synq_absolute_path}/sr_rehydrate")
    assert ok, payload
