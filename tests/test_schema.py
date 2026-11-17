"""The schema of a syncable type, as the core reads it (format 3)."""

import enum
from collections.abc import AsyncIterator, Generator, Iterator
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

import jsonschema
import pytest
from psygnal.containers import EventedDict, EventedList, EventedSet

from SpiriSynq import _core
from SpiriSynq.link import Link
from SpiriSynq.remote_callables import Query, Reply, remote_method, remote_method_raw
from SpiriSynq.schema import (
    INT_KEY_PATTERN,
    JSON_SCHEMA_DIALECT,
    JsonSchema,
    _json_value,
    get_schema,
    type_id,
)
from SpiriSynq.syncable_objects import SubSyncableDataclass, SyncableObject


class Mode(enum.Enum):
    IDLE = "idle"
    BUSY = 2


class Frame(str):
    yaml_tag = "!Frame"


@dataclass
class Gps(SubSyncableDataclass):
    """Where it is."""

    latitude: float = 0.0


@dataclass
class Point:
    x: float = 0.0
    tag: bytes = b""


class Info(TypedDict, total=False):
    name: str


@dataclass
class Camera(SyncableObject):
    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000010:1"


@dataclass
class Base(SyncableObject):
    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000011:2"
    voltage: float = 0.0


@dataclass
class Unnamed(Base):
    pass


@dataclass
class Robot(Unnamed):
    """A robot."""

    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000012:1"
    synq_skip_rehydrate = {"image"}
    name: str = field(default="r", metadata={"help": "Its name."})
    image: bytes = b""
    mode: Mode = Mode.IDLE
    literal: Literal["a", 1] = "a"
    maybe: int | None = None
    either: int | str = 0
    gps: Gps | None = None
    point: Point = field(default_factory=Point)
    points: list[Point] = field(default_factory=list)
    pair: tuple[int, str] = (1, "a")
    many: tuple[int, ...] = ()
    nothing: tuple[()] = ()
    bare_tuple: tuple = ()
    bag: set[int] = field(default_factory=set)
    by_cell: dict[int, float] = field(default_factory=dict)
    by_name: dict[str, Any] = field(default_factory=dict)
    camera: Camera | None = None
    cameras: list[Camera] = field(default_factory=list)
    link: Link | None = None
    frame: Frame = Frame("map")
    evented: EventedList = field(default_factory=EventedList)
    evented_set: EventedSet = field(default_factory=EventedSet)
    evented_dict: EventedDict = field(default_factory=EventedDict)
    info: Info = field(default_factory=dict)
    annotated: Annotated[int, "meta"] = 0
    opaque: object = None
    untyped_list: list = field(default_factory=list)
    unsafe: list = field(default_factory=lambda: [1])

    @remote_method(key="read/{path:**}")
    def read(self, path: str, start: int = 0, query: Query | None = None) -> bytes:
        """Read a file."""
        return b""

    @remote_method
    def stream(self) -> Iterator[int]:
        yield 1

    @remote_method
    def gen(self) -> Generator[int, None, str]:
        yield 1
        return "x"

    @remote_method
    def plain(self, *args, mode: Mode = Mode.BUSY, extra=object()):
        return None

    @remote_method_raw
    async def raw(self, query: Query) -> AsyncIterator[Reply]:
        yield Reply()

    @remote_method_raw
    async def joined(self, query: Query) -> AsyncIterator[Reply]:
        yield Reply()

    @joined.after_call
    async def joined(self, replies) -> AsyncIterator[str]:
        yield ""

    @remote_method
    def schema_of(self) -> JsonSchema:
        return {}

    @remote_method
    def odd_return(self) -> int:
        return 1

    @odd_return.after_call
    async def odd_return(self, replies) -> int:
        yield 1


def test_the_root():
    schema = get_schema(Robot)
    assert schema["$schema"] == JSON_SCHEMA_DIALECT
    assert schema["x-spirisynq-schema"] == 3
    assert schema["$id"] == "urn:uuid:1a2b3c4d-0000-4000-8000-000000000012:1"
    assert schema["x-bases"] == ["urn:uuid:1a2b3c4d-0000-4000-8000-000000000011:2"]
    assert schema["description"] == "A robot."
    assert get_schema(Robot) is schema  # cached
    _core.Schema(schema)  # the core accepts it
    jsonschema.Draft202012Validator.check_schema(schema)


def test_fields():
    props = get_schema(Robot)["properties"]
    assert props["voltage"] == {"type": "number", "default": 0.0}
    assert props["name"]["description"] == "Its name."
    assert props["image"] == {
        "type": "string",
        "contentEncoding": "base64",
        "x-yaml-tag": "!!binary",
        "default": "",
        "x-skip-rehydrate": True,
    }
    assert props["mode"]["enum"] == ["idle", 2]
    assert props["literal"] == {"type": ["string", "integer"], "enum": ["a", 1], "default": "a"}
    assert props["maybe"]["type"] == ["integer", "null"]
    assert props["either"]["anyOf"] == [{"type": "integer"}, {"type": "string"}]
    assert props["gps"]["anyOf"] == [{"$ref": "#/$defs/Gps"}, {"type": "null"}]
    assert props["pair"]["prefixItems"] == [{"type": "integer"}, {"type": "string"}]
    assert props["many"]["items"] == {"type": "integer"}
    assert props["nothing"]["maxItems"] == 0
    assert props["bare_tuple"]["type"] == "array"
    assert props["bag"]["uniqueItems"] is True
    assert props["by_cell"]["propertyNames"] == {"pattern": INT_KEY_PATTERN}
    assert props["by_cell"]["x-key-type"] == "integer"
    assert props["camera"]["type"] == ["string", "null"]
    assert props["camera"]["x-link"] == type_id(Camera)
    assert props["cameras"]["items"]["x-link"] == type_id(Camera)
    assert props["link"]["x-yaml-tag"] == "!Link"
    assert props["frame"]["x-yaml-tag"] == "!Frame"
    assert props["evented_set"]["uniqueItems"] is True
    assert props["evented_dict"]["type"] == "object"
    assert props["annotated"]["type"] == "integer"
    assert props["opaque"] == {"default": None}
    assert "default" not in props["unsafe"]
    defs = get_schema(Robot)["$defs"]
    assert defs["Gps"]["x-sync"] == "fields"
    assert defs["Gps"]["description"] == "Where it is."
    assert defs["Point"]["x-yaml-tag"] == "!Point"
    assert "x-sync" not in defs["Point"]
    assert defs["Info"]["required"] == []


def test_remote_methods():
    methods = get_schema(Robot)["x-remote-methods"]
    read = methods["read"]
    assert read["x-key"] == "read/{path:**}"
    assert read["description"] == "Read a file."
    assert set(read["parameters"]["properties"]) == {"path", "start"}
    assert read["parameters"]["required"] == ["path"]
    assert methods["stream"]["x-generator"] is True
    assert methods["stream"]["returns"] == {"type": "integer"}
    assert methods["gen"]["returns"] == {"type": "integer"}
    assert methods["plain"]["parameters"]["properties"]["mode"] == {
        "enum": ["idle", 2],
        "x-python-type": "Mode",
        "default": 2,
    }
    assert "default" not in methods["plain"]["parameters"]["properties"]["extra"]
    assert methods["raw"]["returns"] == {}
    assert methods["joined"]["returns"] == {"type": "string"}
    assert methods["schema_of"]["returns"] == {"$ref": JSON_SCHEMA_DIALECT}
    assert methods["odd_return"]["returns"] == {}


def test_documents_for_plain_classes():
    schema = get_schema(Point)
    assert schema["properties"]["x"] == {"type": "number", "default": 0.0}
    assert get_schema(Info)["properties"] == {"name": {"type": "string"}}
    assert "$defs" not in schema

    @dataclass
    class Nested:
        point: Point

    assert get_schema(Nested)["$defs"]["Point"]["title"] == "Point"


def test_types_with_the_same_name_get_their_own_defs():
    def make():
        @dataclass
        class Point:
            y: int = 0

        return Point

    @dataclass
    class Both:
        a: Point
        b: make()  # type: ignore[valid-type]

    defs = get_schema(Both)["$defs"]
    assert set(defs) == {"Point", "Point2"}


def test_recursive_types_end():
    @dataclass
    class Tree:
        children: "list[Tree]" = field(default_factory=list)

    Tree.__annotations__["children"] = list[Tree]
    assert get_schema(Tree)["$defs"]["Tree"]["properties"]["children"]["items"] == {
        "$ref": "#/$defs/Tree"
    }


def test_type_ids():
    from SpiriSynq.session import Session

    assert type_id(Session).startswith("urn:pypi:SpiriSynq:")
    assert type_id(Session).endswith(":SpiriSynq.session.Session")

    class Full:
        synq_type_id = "urn:example:thing"

    assert type_id(Full) == "urn:example:thing"
    with pytest.raises(TypeError, match="no type identity"):
        type_id(Unnamed)


def test_json_values():
    assert _json_value(Mode.BUSY) == 2
    assert _json_value(Frame("x")) == "x"
    assert _json_value(True) is True
    assert _json_value(b"\x00") == "AA=="
    assert _json_value(Link("a/b")) == "a/b"
    assert _json_value({3, 1}) == [1, 3]
    assert _json_value({1, "a"}) in ([1, "a"], ["a", 1])
    assert _json_value({"a": [Point()]}) == {"a": [{"x": 0.0, "tag": ""}]}
    assert _json_value({1: 2}) == {"1": 2}
    assert _json_value({(1,): 2}) is not None
    assert _json_value([object()]) is not None and _json_value([object()]) != []
    from SpiriSynq.schema import _NO_VALUE

    assert _json_value({"a": object()}) is _NO_VALUE
    assert _json_value(object()) is _NO_VALUE
    assert _json_value(EventedDict({"a": 1})) == {"a": 1}

    class MyInt(int):
        pass

    assert type(_json_value(MyInt(3))) is int


def test_more_annotations():
    from typing import TypeVar

    T = TypeVar("T")

    class Doc(TypedDict):
        """A documented dict."""

        a: int

    @dataclass
    class More:
        a: int | str | None = None
        b: Literal["x"] | None = None
        c: Literal[None, b"x", 1.5, True] = None
        d: complex = 0j
        e: type[int] = int
        doc: Doc | None = None
        g: list[T] = field(default_factory=list)  # type: ignore[valid-type]

    props = get_schema(More)["properties"]
    assert props["a"]["anyOf"][-1] == {"type": "null"}
    assert props["b"] == {"type": ["string", "null"], "enum": ["x", None], "default": None}
    assert props["c"]["type"] == ["null", "string", "number", "boolean"]
    assert props["d"] == {"x-python-type": "complex"}
    assert props["e"] == {"default": None} or "default" not in props["e"]
    assert props["g"]["items"] == {}
    assert get_schema(More)["$defs"]["Doc"]["description"] == "A documented dict."
