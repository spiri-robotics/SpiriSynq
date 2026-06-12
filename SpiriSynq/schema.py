import dataclasses
import typing
import types as _types
from typing import get_args, get_origin, Union


def get_schema(cls: type) -> dict:
    """Generate a JSON Schema-compatible dict for a dataclass."""
    from SpiriSynq.syncable_objects import SyncableObject

    defs = {}

    def resolve_type(t: type) -> dict:
        origin = get_origin(t)
        args = get_args(t)

        if origin is Union or origin is _types.UnionType:
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                return resolve_type(non_none[0])
            return {"anyOf": [resolve_type(a) for a in non_none]}
        if origin is list:
            return {"type": "array", "items": resolve_type(args[0]) if args else {}}
        if origin is dict:
            return {"type": "object", "additionalProperties": resolve_type(args[1]) if len(args) > 1 else {}}
        if typing.is_typeddict(t):
            if t.__name__ not in defs:
                defs[t.__name__] = {}
                defs[t.__name__] = _build_typeddict_schema(t)
            return {"$ref": f"#/$defs/{t.__name__}"}
        if dataclasses.is_dataclass(t):
            if t.__name__ not in defs:
                defs[t.__name__] = {}
                defs[t.__name__] = _build_schema(t)
            return {"$ref": f"#/$defs/{t.__name__}"}
        return {"type": type_name(t)}

    def _build_typeddict_schema(c: type) -> dict:
        hints = typing.get_type_hints(c)
        required = list(c.__required_keys__)
        properties = {k: resolve_type(v) for k, v in hints.items()}
        schema: dict = {"type": "object", "properties": properties}
        if c.__doc__:
            schema["description"] = c.__doc__
        if required:
            schema["required"] = required
        return schema

    def _build_schema(c: type) -> dict:
        properties = {}
        required = []

        hints = typing.get_type_hints(c)
        field_map = {f.name: f for f in dataclasses.fields(c)}

        if isinstance(c, type) and issubclass(c, SyncableObject):
            # Only include fields that are valid sync paths (filters synq_* internals)
            field_names = {p for p in c.valid_sync_paths() if "/" not in p}
        else:
            field_names = {f.name for f in dataclasses.fields(c)}

        for name in sorted(field_names):
            f = field_map.get(name)
            if f is None:
                continue
            annotation = hints.get(name, f.type)
            optional = _is_optional(annotation)
            resolved_t = _unwrap_optional(annotation) if optional else annotation

            entry = resolve_type(resolved_t)
            if f.metadata.get("help"):
                entry["description"] = f.metadata["help"]
            properties[name] = entry
            if not optional:
                required.append(name)

        schema: dict = {"type": "object", "properties": properties}
        if c.__doc__:
            schema["description"] = c.__doc__
        if required:
            schema["required"] = required
        return schema

    root_schema = _build_schema(cls)

    if isinstance(cls, type) and issubclass(cls, SyncableObject):
        from SpiriSynq.remote_callables import RemoteMethod
        import inspect
        rpc = {}
        for name in dir(cls):
            value = getattr(cls, name, None)
            if not isinstance(value, RemoteMethod):
                continue
            sig = inspect.signature(value._wrapped)
            params = {}
            for pname, p in sig.parameters.items():
                if pname == "self":
                    continue
                entry: dict = {}
                if p.annotation is not inspect.Parameter.empty:
                    entry.update(resolve_type(p.annotation))
                if p.default is not inspect.Parameter.empty:
                    entry["default"] = p.default
                params[pname] = entry
            endpoint: dict = {}
            if value._wrapped.__doc__:
                endpoint["description"] = value._wrapped.__doc__.strip()
            if params:
                endpoint["parameters"] = params
            if sig.return_annotation not in (inspect.Parameter.empty, None, type(None)):
                endpoint["returns"] = resolve_type(sig.return_annotation)
            rpc[name] = endpoint
        if rpc:
            root_schema["x-rpc-endpoints"] = rpc

    if defs:
        root_schema["$defs"] = defs

    return root_schema


def type_name(t: type) -> str:
    return {int: "integer", float: "number", str: "string", bool: "boolean"}.get(t, t.__name__)


def _is_optional(t: type) -> bool:
    origin = get_origin(t)
    return (origin is Union or origin is _types.UnionType) and type(None) in get_args(t)


def _unwrap_optional(t: type) -> type:
    return next(a for a in get_args(t) if a is not type(None))
