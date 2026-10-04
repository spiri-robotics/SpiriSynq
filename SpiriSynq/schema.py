"""JSON Schema (2020-12) generation for synced dataclasses.

The format is part of the wire protocol (``sr_object_schema`` and
``sr_type_schema``); see docs/protocol.md before changing it, and bump
``SCHEMA_VERSION`` for incompatible changes.
"""
import base64
import collections.abc
import dataclasses
import enum
import inspect
import re
import types as _types
import typing
from typing import Literal, NewType, Union, get_args, get_origin

from psygnal.containers import EventedDict, EventedList, EventedSet

SCHEMA_VERSION = 2
"""Value of ``x-spirisynq-schema`` on every root schema."""

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

JsonSchema = NewType("JsonSchema", dict)
"""Return annotation for methods that return a JSON Schema document."""

_PRIMITIVES = (  # order matters: bool is a subclass of int
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
)

_GENERATOR_ORIGINS = {
    collections.abc.Generator,
    collections.abc.Iterator,
    collections.abc.Iterable,
    collections.abc.AsyncGenerator,
    collections.abc.AsyncIterator,
    collections.abc.AsyncIterable,
}

# default_factory callables that are safe to call for a field's ``default``.
_SAFE_FACTORIES = {list, dict, set, frozenset, tuple, str, int, float, bool, bytes,
                   EventedList, EventedDict, EventedSet}

_NO_VALUE = object()


def get_schema(cls: type, codecs=None) -> dict:
    """Generate the JSON Schema for a dataclass, TypedDict or SyncableObject.

    *codecs* is the session's codec list, used to mark fields whose puts are
    not YAML (``x-encoding``); defaults to the built-in codecs.
    """
    return _Builder(codecs).root(cls)


def yaml_tag(t: type) -> str:
    """The YAML tag a dataclass is sent with, as ``register_type_recursive`` assigns it."""
    return getattr(t, "yaml_tag", f"!{t.__name__}")


class _Builder:
    def __init__(self, codecs):
        if codecs is None:
            from SpiriSynq.codecs import BUILTIN_CODECS
            codecs = BUILTIN_CODECS
        self.codecs = codecs
        self.defs: dict[str, dict] = {}

    # --- documents ---------------------------------------------------------

    def root(self, cls: type) -> dict:
        from SpiriSynq.syncable_objects import SyncableObject

        schema = {"$schema": JSON_SCHEMA_DIALECT, "x-spirisynq-schema": SCHEMA_VERSION}
        if typing.is_typeddict(cls):
            schema.update(self.typeddict(cls))
        else:
            schema.update(self.dataclass(cls))
        if isinstance(cls, type) and issubclass(cls, SyncableObject):
            schema["x-classes"] = cls.synq_type_tags()
            schema["x-rpc-endpoints"] = self.rpc_endpoints(cls)
        if self.defs:
            schema["$defs"] = self.defs
        return schema

    def dataclass(self, c: type) -> dict:
        from SpiriSynq.syncable_objects import SyncableObject

        hints = _type_hints(c)
        field_map = {f.name: f for f in dataclasses.fields(c)}
        if issubclass(c, SyncableObject):
            # Only fields that can be published; filters synq_* internals,
            # including the synq_topic/synq_base_topic addressing fields.
            names = {p for p in c.valid_sync_paths() if "/" not in p}
            names = {n for n in names if not n.startswith("synq_")}
        else:
            names = set(field_map)

        properties = {}
        for name in sorted(names):
            f = field_map.get(name)
            if f is None:
                continue
            annotation = hints.get(name, f.type)
            entry = self.resolve(annotation)
            if f.metadata.get("help"):
                entry["description"] = f.metadata["help"]
            default = _field_default(f)
            if default is not _NO_VALUE:
                entry["default"] = default
            encoding = self.encoding_for(annotation)
            if encoding:
                entry["x-encoding"] = encoding
            properties[name] = entry

        schema: dict = {"title": c.__name__, "type": "object", "x-yaml-tag": yaml_tag(c)}
        if doc := _docstring(c):
            schema["description"] = doc
        schema["properties"] = properties
        # Every field is always present (rehydrate sends them all); nullability
        # is expressed in each field's type instead.
        schema["required"] = list(properties)
        return schema

    def typeddict(self, c: type) -> dict:
        hints = _type_hints(c)
        schema: dict = {"title": c.__name__, "type": "object"}
        if doc := _docstring(c):
            schema["description"] = doc
        schema["properties"] = {k: self.resolve(v) for k, v in hints.items()}
        schema["required"] = sorted(c.__required_keys__)
        return schema

    def rpc_endpoints(self, cls: type) -> dict:
        from SpiriSynq.remote_callables import RemoteMethod

        endpoints = {}
        for attr in dir(cls):
            value = getattr(cls, attr, None)
            if not isinstance(value, RemoteMethod):
                continue
            # A method's .client()/.server() hooks return the same RemoteMethod
            # under a second attribute name; it is served under its own name.
            name = value._wrapped.__name__
            if name in endpoints:
                continue
            endpoints[name] = self.endpoint(value)
        return dict(sorted(endpoints.items()))

    def endpoint(self, method) -> dict:
        func = method._wrapped
        sig = inspect.signature(func)
        hints = _type_hints(func)

        properties, required = {}, []
        for pname, p in sig.parameters.items():
            if pname == "self":
                continue
            entry = self.resolve(hints[pname]) if pname in hints else {}
            if p.default is inspect.Parameter.empty:
                required.append(pname)
            else:
                default = _json_value(p.default)
                if default is not _NO_VALUE:
                    entry["default"] = default
            properties[pname] = entry

        endpoint: dict = {}
        if doc := _docstring(func):
            endpoint["description"] = doc
        endpoint["parameters"] = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

        return_t = hints.get("return", _NO_VALUE)
        if method._is_generator or method._is_async_gen:
            yields, returns = self.generator_types(return_t, method._is_async_gen)
            endpoint["x-generator"] = True
            endpoint["yields"] = yields
            endpoint["returns"] = returns
        else:
            endpoint["returns"] = {} if return_t is _NO_VALUE else self.resolve(return_t)
        return endpoint

    def generator_types(self, return_t, is_async: bool) -> tuple[dict, dict]:
        """(yields, returns) schemas for a generator method's return annotation."""
        null = {"type": "null"}
        if return_t is _NO_VALUE:
            return {}, null if is_async else {}
        origin, args = get_origin(return_t), get_args(return_t)
        if origin not in _GENERATOR_ORIGINS:
            return {}, {}
        yields = self.resolve(args[0]) if args else {}
        if origin is collections.abc.Generator and len(args) == 3:
            return yields, self.resolve(args[2])
        return yields, null

    # --- types -------------------------------------------------------------

    def resolve(self, t) -> dict:
        if t is typing.Any or t is object:
            return {}
        if t is None or t is type(None):
            return {"type": "null"}
        if t is typing.Self:
            return {"$ref": "#"}
        if t is JsonSchema:
            return {"$ref": JSON_SCHEMA_DIALECT}

        origin, args = get_origin(t), get_args(t)

        if origin is Literal:
            return _literal(args)
        if origin is Union or origin is _types.UnionType:
            members = [a for a in args if a is not type(None)]
            nullable = len(members) < len(args)
            if len(members) == 1:
                schema = self.resolve(members[0])
                return _nullable(schema) if nullable else schema
            any_of = [self.resolve(a) for a in members]
            if nullable:
                any_of.append({"type": "null"})
            return {"anyOf": any_of}
        if origin is list or origin is collections.abc.Sequence:
            return {"type": "array", "items": self.resolve(args[0]) if args else {}}
        if origin is tuple:
            if len(args) == 2 and args[1] is Ellipsis:
                return {"type": "array", "items": self.resolve(args[0])}
            if args == ((),):  # tuple[()]
                return {"type": "array", "maxItems": 0}
            if args:
                return {
                    "type": "array",
                    "prefixItems": [self.resolve(a) for a in args],
                    "items": False,
                    "minItems": len(args),
                }
            return {"type": "array"}
        if origin in (set, frozenset, collections.abc.Set):
            return {"type": "array", "uniqueItems": True, "x-yaml-tag": "!!set",
                    "items": self.resolve(args[0]) if args else {}}
        if origin is dict or origin is collections.abc.Mapping:
            return {"type": "object",
                    "additionalProperties": self.resolve(args[1]) if len(args) > 1 else {}}
        if origin is not None:
            return self.resolve(origin)

        if typing.is_typeddict(t):
            return self.ref(t, self.typeddict)
        if dataclasses.is_dataclass(t) and isinstance(t, type):
            return self.ref(t, self.dataclass)
        if isinstance(t, type):
            return self.resolve_class(t)
        return {}  # forward refs, TypeVars, ...: accept anything

    def resolve_class(self, t: type) -> dict:
        if issubclass(t, enum.Enum):
            values = [_json_value(m.value) for m in t]
            return {"enum": values, "x-python-type": t.__name__}
        if issubclass(t, (bytes, bytearray)):
            return {"type": "string", "contentEncoding": "base64", "x-yaml-tag": "!!binary"}
        if issubclass(t, EventedList):
            return {"type": "array", "x-yaml-tag": "!EventedList"}
        if issubclass(t, EventedSet):
            return {"type": "array", "uniqueItems": True, "x-yaml-tag": "!EventedSet"}
        if issubclass(t, EventedDict):
            return {"type": "object", "x-yaml-tag": "!EventedDict"}
        for base, name in _PRIMITIVES:
            if issubclass(t, base):
                schema = {"type": name}
                # A primitive subclass (e.g. RootFrame(str)) may carry its own tag.
                if t is not base and hasattr(t, "yaml_tag"):
                    schema["x-yaml-tag"] = t.yaml_tag
                return schema
        if issubclass(t, (list, tuple)):
            return {"type": "array"}
        if issubclass(t, (set, frozenset)):
            return {"type": "array", "uniqueItems": True, "x-yaml-tag": "!!set"}
        if issubclass(t, dict):
            return {"type": "object"}
        # Opaque application type: no structure we can describe.
        schema = {"x-python-type": t.__name__}
        if hasattr(t, "yaml_tag"):
            schema["x-yaml-tag"] = t.yaml_tag
        return schema

    def ref(self, t: type, build) -> dict:
        if t.__name__ not in self.defs:
            self.defs[t.__name__] = {}  # placeholder: recursive types terminate
            self.defs[t.__name__] = build(t)
        return {"$ref": f"#/$defs/{t.__name__}"}

    def encoding_for(self, annotation) -> str | None:
        """Zenoh encoding of field puts for *annotation*, if a codec replaces YAML."""
        origin, args = get_origin(annotation), get_args(annotation)
        candidates = args if origin is Union or origin is _types.UnionType else (annotation,)
        for t in candidates:
            if not isinstance(t, type):
                continue
            for codec in self.codecs:
                python_type = getattr(codec, "python_type", None)
                if isinstance(python_type, type) and issubclass(t, python_type):
                    return str(codec.zenoh_schema)
        return None


# --- helpers ----------------------------------------------------------------


def _type_hints(obj) -> dict:
    """Resolved annotations, or {} if any can't be resolved (e.g. a forward
    reference to a class defined inside a function). Unresolved annotations
    then fall back to the raw string, which the schema treats as "any"."""
    try:
        return typing.get_type_hints(obj)
    except Exception:
        return {}


def _json_type(value) -> str:
    if value is None:
        return "null"
    for base, name in _PRIMITIVES:
        if isinstance(value, base):
            return name
    return "string"


def _literal(values) -> dict:
    enum_values = [_json_value(v) for v in values]
    types = list(dict.fromkeys(_json_type(v) for v in enum_values))
    return {"type": types[0] if len(types) == 1 else types, "enum": enum_values}


def _nullable(schema: dict) -> dict:
    """Allow ``null`` in *schema*: widen a plain ``type``, else wrap in anyOf."""
    t = schema.get("type")
    if t is None or "$ref" in schema or "anyOf" in schema:
        return {"anyOf": [schema, {"type": "null"}]}
    schema = dict(schema)
    types = t if isinstance(t, list) else [t]
    if "null" not in types:
        schema["type"] = [*types, "null"]
    if "enum" in schema and None not in schema["enum"]:
        schema["enum"] = [*schema["enum"], None]
    return schema


def _docstring(obj) -> str | None:
    """The object's own docstring, cleaned; None for dataclass auto-docstrings."""
    doc = obj.__dict__.get("__doc__") if isinstance(obj, type) else obj.__doc__
    if not doc:
        return None
    if isinstance(obj, type) and dataclasses.is_dataclass(obj):
        # @dataclass sets __doc__ to "Name(field: type = default, ...)" when missing.
        if re.fullmatch(rf"{re.escape(obj.__name__)}\(.*\)", doc.strip(), re.S):
            return None
    return inspect.cleandoc(doc) or None


def _field_default(f: dataclasses.Field):
    if f.default is not dataclasses.MISSING:
        return _json_value(f.default)
    if f.default_factory in _SAFE_FACTORIES:  # type: ignore[comparison-overlap]
        return _json_value(f.default_factory())  # type: ignore[misc]
    return _NO_VALUE


def _json_value(value):
    """*value* as plain JSON data, or _NO_VALUE if it has no JSON form."""
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, enum.Enum):
        return _json_value(value.value)
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, (list, tuple, set, frozenset, EventedList, EventedSet)):
        items = [_json_value(v) for v in value]
        if _NO_VALUE in items:
            return _NO_VALUE
        if isinstance(value, (set, frozenset, EventedSet)):
            try:
                items.sort()
            except TypeError:
                pass
        return items
    if isinstance(value, (dict, EventedDict)):
        out = {}
        for k, v in value.items():
            v = _json_value(v)
            if not isinstance(k, str) or v is _NO_VALUE:
                return _NO_VALUE
            out[k] = v
        return out
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_value({f.name: getattr(value, f.name) for f in dataclasses.fields(value)})
    return _NO_VALUE
