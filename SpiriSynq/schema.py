import dataclasses
import typing


def get_schema(cls: type) -> dict:
    """Generate a JSON Schema-compatible dict for a dataclass."""

    defs = {}

    def resolve_type(t: type) -> dict:
        origin = typing.get_origin(t)
        args = typing.get_args(t)

        if origin is list:
            item_type = args[0] if args else {}  # fallback to empty schema if unparameterized
            return {"type": "array", "items": resolve_type(item_type) if args else {}}
        if origin is dict:
            val_type = args[1] if len(args) > 1 else {}
            return {"type": "object", "additionalProperties": resolve_type(val_type) if len(args) > 1 else {}}
        if dataclasses.is_dataclass(t):
            if t.__name__ not in defs:
                defs[t.__name__] = {}
                defs[t.__name__] = _build_schema(t)
            return {"$ref": f"#/$defs/{t.__name__}"}
        return {"type": type_name(t)}


    def _build_schema(c: type) -> dict:
        properties = {}
        required = []

        for f in dataclasses.fields(c):
            t = f.type if not isinstance(f.type, str) else eval(f.type)
            optional = is_optional(t)
            resolved_t = unwrap_optional(t) if optional else t

            entry = resolve_type(resolved_t)

            if f.metadata.get("help"):
                entry["description"] = f.metadata["help"]

            properties[f.name] = entry
            if not optional:
                required.append(f.name)

        schema = {
            "type": "object",
            "properties": properties,
        }
        if c.__doc__:
            schema["description"] = c.__doc__
        if required:
            schema["required"] = required

        return schema

    def type_name(t: type) -> str:
        return {
            int: "integer",
            float: "number",
            str: "string",
            bool: "boolean",
        }.get(t, t.__name__)

    def is_optional(t: type) -> bool:
        return typing.get_origin(t) is typing.Union and type(None) in typing.get_args(t)

    def unwrap_optional(t: type) -> type:
        return next(a for a in typing.get_args(t) if a is not type(None))

    root_schema = _build_schema(cls)

    if defs:
        root_schema["$defs"] = defs

    return root_schema