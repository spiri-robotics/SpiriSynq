"""The JSON Schema (2020-12) of a syncable type, in the format the core reads.

A type's schema is the only description of it that leaves Python: the core works out the
object's keys from it, and other processes and languages build their mirrors from it. See
`rust/ARCHITECTURE.md`, "Schema format".
"""

from __future__ import annotations

import base64
import collections.abc
import contextlib
import dataclasses
import enum
import importlib.metadata
import inspect
import re
import types as _types
import typing
from typing import Any, Literal, NewType, Union, get_args, get_origin

from psygnal.containers import EventedDict, EventedList, EventedSet

from SpiriSynq.link import Link
from SpiriSynq.values import hints_of, tag_of, wire_fields

SCHEMA_VERSION = 3
"""Value of `x-spirisynq-schema` on every root schema."""

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

INT_KEY_PATTERN = "^-?[0-9]+$"
"""`propertyNames` pattern of a mapping with integer keys, which also says `x-key-type`."""

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

_SAFE_FACTORIES = {list, dict, set, frozenset, tuple, str, int, float, bool, bytes,
                   EventedList, EventedDict, EventedSet}  # fmt: skip

_NO_VALUE = object()

BYTES = {"type": "string", "contentEncoding": "base64", "x-yaml-tag": "!!binary"}


# --- type identity --------------------------------------------------------------------------


def type_id(cls: type) -> str:
    """The `$id` of a syncable type: from its own `synq_type_id`, or its PyPI distribution.

    `TypeError`, naming the class, if it has neither.
    """
    own = cls.__dict__.get("synq_type_id")
    if own:
        # `<uuid>:<version>`, or a whole URI of the type's own.
        return own if own.startswith("urn:") or "://" in own else f"urn:uuid:{own}"
    module = cls.__module__
    top = module.split(".", 1)[0]
    names = importlib.metadata.packages_distributions().get(top, [])
    for name in [*names, top]:  # an editable install names no packages: try the module's own
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        return f"urn:pypi:{dist.metadata['Name']}:{dist.version}:{module}.{cls.__qualname__}"
    raise TypeError(
        f"{cls.__qualname__} has no type identity: it isn't in a package installed from PyPI, "
        f"so give it one with synq_type_id = '<uuid>:<version>' (see 'Type identity and "
        f"schemas' in the docs)"
    )


def _has_identity(cls: type) -> bool:
    try:
        type_id(cls)
    except TypeError:
        return False
    return True


# --- the builder ----------------------------------------------------------------------------


def get_schema(cls: type) -> dict:
    """The schema of a syncable type, a dataclass or a TypedDict. Cached per syncable class."""
    from SpiriSynq.syncable_objects import SyncableObject

    if isinstance(cls, type) and issubclass(cls, SyncableObject):
        cached = cls.__dict__.get("_synq_schema")
        if cached is None:
            cached = _Builder().syncable(cls)
            cls._synq_schema = cached
        return cached
    return _Builder().document(cls)


class _Builder:
    def __init__(self):
        self.defs: dict[str, dict] = {}
        self.def_names: dict[type, str] = {}

    def document(self, cls: type) -> dict:
        schema: dict = {"$schema": JSON_SCHEMA_DIALECT}
        schema.update(self.typeddict(cls) if typing.is_typeddict(cls) else self.dataclass(cls))
        if self.defs:
            schema["$defs"] = self.defs
        return schema

    def syncable(self, cls: type) -> dict:
        from SpiriSynq import wasm
        from SpiriSynq.remote_callables import RemoteMethod
        from SpiriSynq.syncable_objects import SyncableObject

        schema: dict = {
            "$schema": JSON_SCHEMA_DIALECT,
            "x-spirisynq-schema": SCHEMA_VERSION,
            "$id": type_id(cls),
        }
        bases = [
            type_id(b)
            for b in cls.__mro__[1:]
            if issubclass(b, SyncableObject) and b is not SyncableObject and _has_identity(b)
        ]
        if bases:
            schema["x-bases"] = bases
        schema.update(self.dataclass(cls))
        methods = {}
        for _, member in members(cls):
            if isinstance(member, RemoteMethod):
                methods[member.name] = self.remote_method(member)
        if methods:
            schema["x-remote-methods"] = dict(sorted(methods.items()))
        code = wasm.entries(cls, self)
        if code:
            schema["x-wasm"] = code
        if self.defs:
            schema["$defs"] = self.defs
        return schema

    def dataclass(self, c: type) -> dict:
        from SpiriSynq.syncable_objects import SubSyncableDataclass, SyncableObject

        hints = hints_of(c)
        fields = c.synq_fields() if issubclass(c, SyncableObject) else wire_fields(c)
        skip_rehydrate: set = set()
        for k in c.__mro__:
            skip_rehydrate |= set(k.__dict__.get("synq_skip_rehydrate", ()))

        properties = {}
        for f in fields:
            entry = self.resolve(hints.get(f.name, f.type))
            if f.metadata.get("help"):
                entry["description"] = f.metadata["help"]
            default = _field_default(f)
            if default is not _NO_VALUE:
                entry["default"] = default
            if f.name in skip_rehydrate:
                entry["x-skip-rehydrate"] = True
            properties[f.name] = entry

        schema: dict = {"title": c.__name__, "type": "object"}
        if not issubclass(c, SyncableObject):
            schema["x-yaml-tag"] = f"!{tag_of(c)}"
        if issubclass(c, SubSyncableDataclass):
            schema["x-sync"] = "fields"
        if doc := _docstring(c):
            schema["description"] = doc
        schema["properties"] = properties
        return schema

    def typeddict(self, c: type) -> dict:
        hints = hints_of(c)
        schema: dict = {"title": c.__name__, "type": "object"}
        if doc := _docstring(c):
            schema["description"] = doc
        schema["properties"] = {k: self.resolve(v) for k, v in hints.items()}
        schema["required"] = sorted(c.__required_keys__)
        return schema

    def parameters(self, func, skip: int, leave_out=()) -> dict:
        """The object schema of `func`'s parameters after the first `skip`."""
        sig = inspect.signature(func)
        hints = hints_of(func)
        properties, required = {}, []
        for pname, p in list(sig.parameters.items())[skip:]:
            if pname in leave_out or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            entry = self.resolve(hints[pname]) if pname in hints else {}
            if p.default is inspect.Parameter.empty:
                required.append(pname)
            else:
                default = _json_value(p.default)
                if default is not _NO_VALUE:
                    entry["default"] = default
            properties[pname] = entry
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def remote_method(self, method) -> dict:
        entry: dict = {}
        if doc := _docstring(method.func):
            entry["description"] = doc
        if method.key is not None:
            entry["x-key"] = method.key
        func, skip = method.caller_signature()
        entry["parameters"] = self.parameters(func, skip, leave_out=method.query_params(func))
        entry["returns"] = self.returns(method)
        if method.is_generator:
            entry["x-generator"] = True
        return entry

    def returns(self, method) -> dict:
        """The schema of each value a caller gets."""
        if method.after_call_func is not None:
            hint = hints_of(method.after_call_func).get("return", _NO_VALUE)
            return self.yields(hint)
        if method.raw:
            return {}  # whatever its replies carry
        hint = hints_of(method.func).get("return", _NO_VALUE)
        if method.is_generator:
            return self.yields(hint)
        return {} if hint is _NO_VALUE else self.resolve(hint)

    def yields(self, hint) -> dict:
        if hint is _NO_VALUE or get_origin(hint) not in _GENERATOR_ORIGINS:
            return {}
        args = get_args(hint)
        return self.resolve(args[0]) if args else {}

    # --- types ------------------------------------------------------------------------------

    def resolve(self, t) -> dict:
        if t is typing.Any or t is object:
            return {}
        if t is None or t is type(None):
            return {"type": "null"}
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
        if origin in (list, collections.abc.Sequence, collections.abc.MutableSequence):
            return {"type": "array", "items": self.resolve(args[0]) if args else {}}
        if origin is tuple:
            if len(args) == 2 and args[1] is Ellipsis:
                return {"type": "array", "items": self.resolve(args[0])}
            if not args:  # tuple[()]: a bare `tuple` has no origin
                return {"type": "array", "maxItems": 0}
            if args:
                return {
                    "type": "array",
                    "prefixItems": [self.resolve(a) for a in args],
                    "items": False,
                    "minItems": len(args),
                }
        if origin in (set, frozenset, collections.abc.Set, collections.abc.MutableSet):
            return {"type": "array", "uniqueItems": True,
                    "items": self.resolve(args[0]) if args else {}}  # fmt: skip
        if origin in (dict, collections.abc.Mapping, collections.abc.MutableMapping):
            values = self.resolve(args[1]) if len(args) > 1 else {}
            schema = {"type": "object", "additionalProperties": values}
            if args and args[0] is int:
                # JSON's keys are strings: say which strings, and that they stand for integers.
                schema["propertyNames"] = {"pattern": INT_KEY_PATTERN}
                schema["x-key-type"] = "integer"
            return schema
        if origin is not None:
            return self.resolve(origin)
        if isinstance(t, type):
            return self.resolve_class(t)
        return {}  # forward references, TypeVars...: anything

    def resolve_class(self, t: type) -> dict:
        from SpiriSynq.syncable_objects import SyncableObject

        if issubclass(t, SyncableObject):
            return {"type": "string", "x-yaml-tag": "!Link", "x-link": type_id(t)}
        if issubclass(t, Link):
            return {"type": "string", "x-yaml-tag": "!Link"}
        if typing.is_typeddict(t):
            return self.ref(t, self.typeddict)
        if dataclasses.is_dataclass(t):
            return self.ref(t, self.dataclass)
        if issubclass(t, enum.Enum):
            values = [_json_value(m.value) for m in t]
            return {"enum": values, "x-python-type": t.__name__}
        if issubclass(t, (bytes, bytearray)):
            return dict(BYTES)
        if issubclass(t, (EventedList, list, tuple)):
            return {"type": "array"}
        if issubclass(t, (EventedSet, set, frozenset)):
            return {"type": "array", "uniqueItems": True}
        if issubclass(t, (EventedDict, dict)):
            return {"type": "object"}
        for base, name in _PRIMITIVES:
            if issubclass(t, base):
                schema = {"type": name}
                # A primitive subclass (e.g. RootFrame(str)) may carry its own tag.
                if t is not base and "yaml_tag" in t.__dict__:
                    schema["x-yaml-tag"] = f"!{tag_of(t)}"
                return schema
        # An application type with its own wire form: nothing we can describe.
        return {"x-python-type": t.__name__}

    def ref(self, t: type, build) -> dict:
        name = self.def_names.get(t)
        if name is None:
            name = t.__name__
            n = 2
            while name in self.defs:
                name, n = f"{t.__name__}{n}", n + 1
            self.def_names[t] = name
            self.defs[name] = {}  # a placeholder, so recursive types end
            self.defs[name] = build(t)
        return {"$ref": f"#/$defs/{name}"}


def members(cls: type):
    """`(name, attribute)` for each attribute of `cls` and its bases, the nearest first."""
    seen = set()
    for klass in cls.__mro__:
        for name, member in klass.__dict__.items():
            if name not in seen:
                seen.add(name)
                yield name, member


# --- helpers --------------------------------------------------------------------------------


def _json_type(value) -> str:
    """The JSON type of a literal's value: `null`, a primitive's, or `string` (bytes)."""
    names = [name for base, name in _PRIMITIVES if isinstance(value, base)]
    return "null" if value is None else (names or ["string"])[0]


def _literal(values) -> dict:
    enum_values = [_json_value(v) for v in values]
    types = list(dict.fromkeys(_json_type(v) for v in enum_values))
    return {"type": types[0] if len(types) == 1 else types, "enum": enum_values}


def _nullable(schema: dict) -> dict:
    """Allow `null` in `schema`: widen a plain `type`, else wrap it in `anyOf`."""
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
    """The object's own docstring, cleaned; `None` for a dataclass's generated one."""
    doc = obj.__dict__.get("__doc__") if isinstance(obj, type) else getattr(obj, "__doc__", None)
    if not doc:
        return None
    generated = rf"{re.escape(getattr(obj, '__name__', ''))}\(.*\)"
    # @dataclass sets __doc__ to "Name(field: type = default, ...)" when there's none.
    if dataclasses.is_dataclass(obj) and re.fullmatch(generated, doc.strip(), re.S):
        return None
    return inspect.cleandoc(doc) or None


def _field_default(f: dataclasses.Field):
    if f.default is not dataclasses.MISSING:
        return _json_value(f.default)
    if f.default_factory in _SAFE_FACTORIES:  # type: ignore[comparison-overlap]
        return _json_value(f.default_factory())  # type: ignore[misc]
    return _NO_VALUE


def _json_value(value: Any):
    """`value` as plain JSON data, or `_NO_VALUE` if it has no JSON form."""
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, enum.Enum):
        return _json_value(value.value)
    for base in (bool, int, float, str):
        if isinstance(value, base):
            return base(value)
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, Link):
        return value.topic
    if isinstance(value, (list, tuple, set, frozenset, EventedList, EventedSet)):
        items = [_json_value(v) for v in value]
        if _NO_VALUE in items:
            return _NO_VALUE
        if isinstance(value, (set, frozenset, EventedSet)):
            with contextlib.suppress(TypeError):
                items.sort()
        return items
    if isinstance(value, (dict, EventedDict)):
        out = {}
        for k, v in value.items():
            v = _json_value(v)
            if not isinstance(k, (str, int)) or isinstance(k, bool) or v is _NO_VALUE:
                return _NO_VALUE
            out[str(k)] = v
        return out
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_value({f.name: getattr(value, f.name) for f in wire_fields(type(value))})
    return _NO_VALUE
