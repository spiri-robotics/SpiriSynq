import yaml
from yaml import SafeLoader, SafeDumper


class SessionSerializer:
    """Per-session YAML serializer with isolated type registries.

    PyYAML creates a fresh Loader/Dumper instance on every dump()/load() call,
    so there is no shared mutable state between threads. Registered types live
    in class-level dicts on per-session Loader/Dumper subclasses; PyYAML's
    add_constructor/add_representer do copy-on-write on first modification, so
    sessions are fully isolated from each other and from PyYAML's built-ins.
    """

    def __init__(self):
        class _Loader(SafeLoader):
            pass

        class _Dumper(SafeDumper):
            pass

        self._Loader = _Loader
        self._Dumper = _Dumper

    def dumps(self, data) -> str:
        result = yaml.dump(data, Dumper=self._Dumper)
        return result.removesuffix("...\n").removesuffix("\n")

    def load(self, raw):
        return yaml.load(raw, Loader=self._Loader)

    def register_class(self, cls) -> None:
        tag = getattr(cls, "yaml_tag", f"!{cls.__name__}")
        try:
            representer_func = cls.to_yaml
        except AttributeError:
            def representer_func(dumper, data, _tag=tag, _cls=cls):
                return dumper.represent_yaml_object(_tag, data, _cls)
        self._Dumper.add_representer(cls, representer_func)

        try:
            constructor_func = cls.from_yaml
        except AttributeError:
            def constructor_func(loader, node, _cls=cls):
                return loader.construct_yaml_object(node, _cls)
        self._Loader.add_constructor(tag, constructor_func)

    @property
    def representer(self):
        """The per-session Dumper class (holds yaml_representers)."""
        return self._Dumper

    @property
    def constructor(self):
        """The per-session Loader class (holds yaml_constructors)."""
        return self._Loader


# Loader that strips all YAML tags and returns plain dicts/lists.
# Used for deserialising payloads when no type registry is available.
class _TagStrippingLoader(SafeLoader):
    pass

def _construct_any_as_mapping(loader, _tag, node):
    return loader.construct_mapping(node, deep=True)

_TagStrippingLoader.add_multi_constructor("", _construct_any_as_mapping)


def load_untyped(raw: str) -> dict:
    """Load a YAML string ignoring all tags, returning plain dicts."""
    return yaml.load(raw, Loader=_TagStrippingLoader)
