"""
Unit tests for SpiriSynq schema generation.
These tests verify JSON Schema output for dataclasses, TypedDicts, SyncableObjects, and RPC methods.
"""

import dataclasses
from dataclasses import dataclass
from typing import TypedDict

from SpiriSynq.schema import get_schema
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.remote_callables import remote_method


def test_primitive_fields():
    @dataclass
    class Simple:
        count: int = 0
        label: str = ""
        ratio: float = 0.0
        active: bool = False

    schema = get_schema(Simple)
    assert schema["type"] == "object"
    props = schema["properties"]
    assert props["count"] == {"type": "integer"}
    assert props["label"] == {"type": "string"}
    assert props["ratio"] == {"type": "number"}
    assert props["active"] == {"type": "boolean"}


def test_optional_fields_not_required():
    @dataclass
    class WithOptional:
        required_field: str = dataclasses.field(default_factory=str)
        optional_field: str | None = None

    schema = get_schema(WithOptional)
    required = schema.get("required", [])
    assert "optional_field" not in required


def test_list_field():
    @dataclass
    class WithList:
        items: list[int] = dataclasses.field(default_factory=list)

    schema = get_schema(WithList)
    prop = schema["properties"]["items"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "integer"}


def test_dict_field():
    @dataclass
    class WithDict:
        mapping: dict[str, float] = dataclasses.field(default_factory=dict)

    schema = get_schema(WithDict)
    prop = schema["properties"]["mapping"]
    assert prop["type"] == "object"
    assert prop["additionalProperties"] == {"type": "number"}


def test_nested_dataclass_uses_ref():
    @dataclass
    class Inner:
        value: int = 0

    @dataclass
    class Outer:
        inner: Inner | None = None

    schema = get_schema(Outer)
    assert "$defs" in schema
    assert "Inner" in schema["$defs"]
    assert schema["properties"]["inner"] == {"$ref": "#/$defs/Inner"}


def test_typeddict_uses_ref():
    class Config(TypedDict):
        host: str
        port: int

    @dataclass
    class WithConfig:
        config: Config | None = None

    schema = get_schema(WithConfig)
    assert "$defs" in schema
    assert "Config" in schema["$defs"]
    config_schema = schema["$defs"]["Config"]
    assert config_schema["type"] == "object"
    assert "host" in config_schema["properties"]
    assert "port" in config_schema["properties"]


def test_typeddict_required_keys():
    class Strict(TypedDict):
        name: str
        value: int

    @dataclass
    class WithStrict:
        data: Strict | None = None

    schema = get_schema(WithStrict)
    strict_schema = schema["$defs"]["Strict"]
    assert set(strict_schema.get("required", [])) == {"name", "value"}


def test_field_help_metadata():
    @dataclass
    class Documented:
        speed: float = dataclasses.field(default=0.0, metadata={"help": "Speed in m/s"})

    schema = get_schema(Documented)
    assert schema["properties"]["speed"]["description"] == "Speed in m/s"


def test_class_docstring():
    @dataclass
    class Described:
        """A well-documented dataclass."""
        value: int = 0

    schema = get_schema(Described)
    assert schema["description"] == "A well-documented dataclass."


def test_syncable_object_includes_user_fields():
    @dataclass
    class MyObj(SyncableObject):
        speed: float = 0.0
        name: str = ""

    schema = get_schema(MyObj)
    props = schema["properties"]
    assert "speed" in props
    assert "name" in props
    assert props["speed"] == {"type": "number"}
    assert props["name"] == {"type": "string"}


def test_syncable_object_rpc_endpoint():
    @dataclass
    class WithRpc(SyncableObject):
        value: int = 0

        @remote_method
        def set_value(self, new_value: int) -> None:
            """Set the value."""
            self.value = new_value

        @remote_method
        def get_value(self) -> int:
            """Return the value."""
            return self.value

    schema = get_schema(WithRpc)
    assert "x-rpc-endpoints" in schema
    rpc = schema["x-rpc-endpoints"]

    # void return: set_value should have no "returns" key
    assert "set_value" in rpc
    endpoint = rpc["set_value"]
    assert endpoint.get("description") == "Set the value."
    assert "new_value" in endpoint["parameters"]
    assert endpoint["parameters"]["new_value"]["type"] == "integer"
    assert "returns" not in endpoint

    # typed return: get_value should have a "returns" key
    assert "get_value" in rpc
    assert rpc["get_value"].get("returns") == {"type": "integer"}


def test_union_type_any_of():
    @dataclass
    class WithUnion:
        value: int | str = 0

    schema = get_schema(WithUnion)
    prop = schema["properties"]["value"]
    assert "anyOf" in prop
    types = {entry["type"] for entry in prop["anyOf"]}
    assert "integer" in types
    assert "string" in types
