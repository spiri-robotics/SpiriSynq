"""
Unit tests for SpiriSynq schema generation.

Every schema built here is also checked against the JSON Schema 2020-12
meta-schema, so the output stays standard JSON Schema rather than merely
JSON-Schema-shaped.
"""

import base64
import dataclasses
import enum
from dataclasses import dataclass
from typing import AsyncGenerator, Generator, Iterator, Literal, TypedDict

import jsonschema
import pytest
from psygnal.containers import EventedList

from SpiriSynq import schema as schema_module
from SpiriSynq.remote_callables import remote_method
from SpiriSynq.serializer import load_untyped
from SpiriSynq.syncable_objects import SubSyncableDataclass, SyncableObject


def get_schema(cls, **kwargs):
    schema = schema_module.get_schema(cls, **kwargs)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def _to_json(value):
    """YAML-loaded data in its JSON form: bytes as base64, sets as lists."""
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, (set, frozenset)):
        return sorted(_to_json(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    return value


# ── document structure ────────────────────────────────────────────────────────

def test_root_document_markers():
    @dataclass
    class Simple:
        count: int = 0

    schema = get_schema(Simple)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["x-spirisynq-schema"] == schema_module.SCHEMA_VERSION
    assert schema["title"] == "Simple"
    assert schema["type"] == "object"
    assert schema["x-yaml-tag"] == "!Simple"


def test_primitive_fields_with_defaults():
    @dataclass
    class Simple:
        count: int = 3
        label: str = "x"
        ratio: float = 0.5
        active: bool = False

    props = get_schema(Simple)["properties"]
    assert props["count"] == {"type": "integer", "default": 3}
    assert props["label"] == {"type": "string", "default": "x"}
    assert props["ratio"] == {"type": "number", "default": 0.5}
    assert props["active"] == {"type": "boolean", "default": False}


def test_every_field_required_and_optional_is_nullable():
    @dataclass
    class WithOptional:
        required_field: str = dataclasses.field(default_factory=str)
        optional_field: str | None = None

    schema = get_schema(WithOptional)
    assert schema["required"] == ["optional_field", "required_field"]
    assert schema["properties"]["optional_field"] == {"type": ["string", "null"], "default": None}


def test_unsafe_default_factory_is_not_called():
    calls = []

    def factory():
        calls.append(1)
        return 5

    @dataclass
    class WithFactory:
        value: int = dataclasses.field(default_factory=factory)

    calls.clear()  # @dataclass doesn't call it, but be explicit
    assert "default" not in get_schema(WithFactory)["properties"]["value"]
    assert calls == []


def test_containers():
    @dataclass
    class Containers:
        items: list[int] = dataclasses.field(default_factory=list)
        mapping: dict[str, float] = dataclasses.field(default_factory=dict)
        pair: tuple[int, str] = (1, "a")
        many: tuple[int, ...] = ()
        tags: set[str] = dataclasses.field(default_factory=set)
        evented: EventedList = dataclasses.field(default_factory=EventedList)

    props = get_schema(Containers)["properties"]
    assert props["items"] == {"type": "array", "items": {"type": "integer"}, "default": []}
    assert props["mapping"] == {
        "type": "object", "additionalProperties": {"type": "number"}, "default": {}
    }
    assert props["pair"] == {
        "type": "array",
        "prefixItems": [{"type": "integer"}, {"type": "string"}],
        "items": False,
        "minItems": 2,
        "default": [1, "a"],
    }
    assert props["many"] == {"type": "array", "items": {"type": "integer"}, "default": []}
    assert props["tags"] == {
        "type": "array", "uniqueItems": True, "x-yaml-tag": "!!set",
        "items": {"type": "string"}, "default": [],
    }
    assert props["evented"] == {"type": "array", "x-yaml-tag": "!EventedList", "default": []}


def test_literal_becomes_enum():
    @dataclass
    class WithLiteral:
        mode: Literal["a", "b"] = "a"
        maybe: Literal["x", "y"] | None = None
        mixed: Literal[1, "one"] = 1

    props = get_schema(WithLiteral)["properties"]
    assert props["mode"] == {"type": "string", "enum": ["a", "b"], "default": "a"}
    assert props["maybe"] == {
        "type": ["string", "null"], "enum": ["x", "y", None], "default": None
    }
    assert props["mixed"]["type"] == ["integer", "string"]


def test_enum_class():
    class Color(enum.Enum):
        RED = "red"
        BLUE = "blue"

    @dataclass
    class WithEnum:
        color: Color = Color.RED

    assert get_schema(WithEnum)["properties"]["color"] == {
        "enum": ["red", "blue"], "x-python-type": "Color", "default": "red"
    }


def test_union_type_any_of():
    @dataclass
    class WithUnion:
        value: int | str = 0
        maybe: int | str | None = None

    props = get_schema(WithUnion)["properties"]
    assert props["value"]["anyOf"] == [{"type": "integer"}, {"type": "string"}]
    assert props["maybe"]["anyOf"] == [{"type": "integer"}, {"type": "string"}, {"type": "null"}]


def test_str_subclass_keeps_its_yaml_tag():
    class Frame(str):
        yaml_tag = "!Frame"

    @dataclass
    class WithFrame:
        frame: Frame = Frame("world")

    assert get_schema(WithFrame)["properties"]["frame"] == {
        "type": "string", "x-yaml-tag": "!Frame", "default": "world"
    }


def test_opaque_type_is_annotation_only():
    class Blob:
        pass

    @dataclass
    class WithBlob:
        blob: Blob | None = None

    assert get_schema(WithBlob)["properties"]["blob"] == {
        "anyOf": [{"x-python-type": "Blob"}, {"type": "null"}], "default": None
    }


def test_bytes_field_encoding():
    @dataclass
    class WithBytes:
        data: bytes | None = None

    assert get_schema(WithBytes)["properties"]["data"] == {
        "type": ["string", "null"],
        "contentEncoding": "base64",
        "x-yaml-tag": "!!binary",
        "x-encoding": "zenoh/bytes",
        "default": None,
    }


def test_custom_codec_sets_encoding():
    import zenoh

    class Pixels:
        pass

    class PixelsCodec:
        python_type = Pixels
        zenoh_schema = zenoh.Encoding.IMAGE_JPEG

    @dataclass
    class WithPixels:
        frame: Pixels | None = None

    schema = get_schema(WithPixels, codecs=[PixelsCodec()])
    assert schema["properties"]["frame"]["x-encoding"] == "image/jpeg"


# ── nested types ──────────────────────────────────────────────────────────────

def test_nested_dataclass_uses_ref():
    @dataclass
    class Inner:
        """Inner doc."""
        value: int = 0

    @dataclass
    class Outer:
        inner: Inner | None = None
        always: Inner = dataclasses.field(default_factory=Inner)

    schema = get_schema(Outer)
    assert schema["$defs"]["Inner"] == {
        "title": "Inner",
        "type": "object",
        "x-yaml-tag": "!Inner",
        "description": "Inner doc.",
        "properties": {"value": {"type": "integer", "default": 0}},
        "required": ["value"],
    }
    assert schema["properties"]["inner"] == {
        "anyOf": [{"$ref": "#/$defs/Inner"}, {"type": "null"}], "default": None
    }
    # Unknown factory: no default, rather than calling arbitrary code.
    assert schema["properties"]["always"] == {"$ref": "#/$defs/Inner"}


def test_frozen_dataclass_default_is_serialised():
    @dataclass(frozen=True)
    class Point:
        x: float = 0
        y: float = 0

    @dataclass
    class WithPoint:
        origin: Point = Point(1, 2)

    assert get_schema(WithPoint)["properties"]["origin"]["default"] == {"x": 1, "y": 2}


def test_typeddict_required_keys():
    class Strict(TypedDict, total=False):
        name: str
        value: int

    @dataclass
    class WithStrict:
        data: Strict | None = None

    strict = get_schema(WithStrict)["$defs"]["Strict"]
    assert strict["type"] == "object"
    assert "x-yaml-tag" not in strict  # sent as a plain mapping
    assert strict["required"] == []


@dataclass
class _Node:
    child: "_Node | None" = None


def test_recursive_type_terminates():
    schema = get_schema(_Node)
    assert schema["properties"]["child"]["anyOf"][0] == {"$ref": "#/$defs/_Node"}


def test_unresolvable_annotation_accepts_anything():
    @dataclass
    class Local:
        child: "Local | None" = None

    assert get_schema(Local)["properties"]["child"] == {"default": None}


# ── descriptions ──────────────────────────────────────────────────────────────

def test_field_help_metadata():
    @dataclass
    class Documented:
        speed: float = dataclasses.field(default=0.0, metadata={"help": "Speed in m/s"})

    assert get_schema(Documented)["properties"]["speed"]["description"] == "Speed in m/s"


def test_class_docstring_is_dedented():
    @dataclass
    class Described:
        """
        A well-documented dataclass.

            Indented detail.
        """
        value: int = 0

    assert get_schema(Described)["description"] == (
        "A well-documented dataclass.\n\n    Indented detail."
    )


def test_dataclass_auto_docstring_is_omitted():
    @dataclass
    class Undocumented:
        value: int = 0

    assert "description" not in get_schema(Undocumented)


# ── SyncableObject ────────────────────────────────────────────────────────────

def test_syncable_object_fields_and_classes():
    @dataclass
    class MyObj(SyncableObject):
        speed: float = 0.0
        name: str = ""

    schema = get_schema(MyObj)
    assert set(schema["properties"]) == {"speed", "name"}  # no synq_topic/base_topic
    assert schema["x-classes"] == ["!MyObj", "!SyncableObject"]


def test_builtin_endpoints_describe_themselves():
    @dataclass
    class Plain(SyncableObject):
        value: int = 0

    schema = get_schema(Plain)
    rpc = schema["x-rpc-endpoints"]
    assert set(rpc) == {"sr_metadata", "sr_object_schema", "sr_rehydrate"}
    no_params = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    for endpoint in rpc.values():
        assert endpoint["parameters"] == no_params
        assert endpoint["description"]
    assert rpc["sr_rehydrate"]["returns"] == {"$ref": "#"}
    assert rpc["sr_object_schema"]["returns"] == {"$ref": schema_module.JSON_SCHEMA_DIALECT}
    assert rpc["sr_metadata"]["returns"] == {"$ref": "#/$defs/SyncableObjectMetadata"}
    assert schema["$defs"]["SyncableObjectMetadata"]["required"] == [
        "authoritive_node", "classes", "topic"
    ]


def test_rpc_endpoint():
    @dataclass
    class WithRpc(SyncableObject):
        value: int = 0

        @remote_method()
        def set_value(self, new_value: int, ramp: bool = False) -> None:
            """
            Set the value.
            """
            self.value = new_value

        @remote_method()
        def untyped(self, x):
            return x

    rpc = get_schema(WithRpc)["x-rpc-endpoints"]
    assert rpc["set_value"] == {
        "description": "Set the value.",
        "parameters": {
            "type": "object",
            "properties": {
                "new_value": {"type": "integer"},
                "ramp": {"type": "boolean", "default": False},
            },
            "required": ["new_value"],
            "additionalProperties": False,
        },
        "returns": {"type": "null"},
    }
    assert rpc["untyped"]["parameters"]["properties"] == {"x": {}}
    assert rpc["untyped"]["returns"] == {}
    assert "x-generator" not in rpc["set_value"]


def test_generator_endpoints():
    @dataclass
    class WithGen(SyncableObject):
        @remote_method()
        def full(self, n: int) -> Generator[int, None, str]:
            yield n
            return "done"

        @remote_method()
        def iterator(self) -> Iterator[str]:
            yield "a"

        @remote_method()
        async def stream(self) -> AsyncGenerator[float, None]:
            yield 1.0

        @remote_method()
        def bare(self):
            yield 1

    rpc = get_schema(WithGen)["x-rpc-endpoints"]
    assert {k: rpc["full"][k] for k in ("x-generator", "yields", "returns")} == {
        "x-generator": True, "yields": {"type": "integer"}, "returns": {"type": "string"}
    }
    assert rpc["iterator"]["yields"] == {"type": "string"}
    assert rpc["iterator"]["returns"] == {"type": "null"}
    assert rpc["stream"]["yields"] == {"type": "number"}
    assert rpc["stream"]["returns"] == {"type": "null"}
    assert rpc["bare"]["x-generator"] is True
    assert rpc["bare"]["yields"] == {} and rpc["bare"]["returns"] == {}


def test_client_hook_does_not_duplicate_endpoint():
    @dataclass
    class WithClient(SyncableObject):
        @remote_method()
        def ping(self) -> str:
            return "pong"

        @ping.client()
        def ping_client(self, result):
            return result

    rpc = get_schema(WithClient)["x-rpc-endpoints"]
    assert "ping" in rpc and "ping_client" not in rpc
    assert "sr_rehydrate_client" not in rpc


# ── payloads validate against their schema ────────────────────────────────────

@dataclass
class _ValGimbal(SubSyncableDataclass):
    pitch: float = 0.0


@dataclass
class _ValObj(SyncableObject):
    speed: float = 1.5
    count: int | None = None
    mode: Literal["a", "b"] = "b"
    tags: list[str] = dataclasses.field(default_factory=lambda: ["x"])
    pair: tuple[int, int] = (1, 2)
    flags: set[int] = dataclasses.field(default_factory=lambda: {3})
    blob: bytes | None = b"\x00\x01"
    gimbal: _ValGimbal | None = None


@pytest.mark.parametrize("gimbal", [None, _ValGimbal(pitch=2.0)])
def test_rehydrate_payload_validates(zenoh_current_session, gimbal):
    obj = _ValObj(synq_topic="schema_test/validate", synq_authoritive=True, gimbal=gimbal)
    schema = get_schema(_ValObj)
    payload = load_untyped(zenoh_current_session.type_registry.dumps(obj))
    jsonschema.validate(_to_json(payload), schema, cls=jsonschema.Draft202012Validator)

    payload["mode"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_to_json(payload), schema, cls=jsonschema.Draft202012Validator)


def test_example_types_produce_valid_schemas():
    pytest.importorskip("PIL")
    from SpiriSynq.example_types import position, robot

    for cls in (position.Position, robot.GPS, robot.MavGPS, robot.MavBattery,
                robot.Camera, robot.MjpegCamera, robot.MavFcu):
        get_schema(cls)
