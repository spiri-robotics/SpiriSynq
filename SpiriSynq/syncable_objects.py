from SpiriSynq.remote_callables import RemoteMethod, remote_method
from SpiriSynq.session import Session, IsolatedYAML, current_session

import zenoh
from loguru import logger
from psygnal import EmissionInfo, SignalGroupDescriptor


from dataclasses import dataclass, field, fields
from typing import ClassVar

import dataclasses
import types as _types
import typing
from typing import Self, TypedDict, get_args, get_origin, Union
from deepdiff import DeepDiff, Delta

import threading
from contextlib import contextmanager
from psygnal import Signal
import weakref


class SyncableObjectMetadata(TypedDict):
    topic: str
    classes: list[str]
    authoritive_node: str


def _unwrap_dataclass_types(annotation) -> list[type]:
    """
    Given a type annotation (e.g. Bar | None, Optional[Bar], Bar),
    return any contained types that are dataclasses.
    Pure static analysis — no instances involved.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is _types.UnionType:
        return [
            a
            for a in get_args(annotation)
            if isinstance(a, type) and dataclasses.is_dataclass(a)
        ]
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        return [annotation]
    return []


def _collect_valid_sync_paths(
    cls: type,
    skip: set[str],
    prefix: str = "",
    _visited: frozenset = frozenset(),
    root_cls: type | None = None,
    warn: bool = False,
) -> set[str]:
    """
    Recursively collect all valid sync paths for a dataclass class.
    Uses only class-level type annotations — no instance access.

    e.g. {"foo", "bar", "bar/value", "bar/name"}
    """
    if cls in _visited:
        return set()
    _visited = _visited | {cls}

    hints = typing.get_type_hints(cls)
    valid_paths = set()

    for f in dataclasses.fields(cls):
        if f.name in skip or f.name.startswith("_"):
            continue

        path = f"{prefix}/{f.name}" if prefix else f.name
        valid_paths.add(path)

        annotation = hints.get(f.name, f.type)
        for nested_cls in _unwrap_dataclass_types(annotation):
            if warn:
                is_frozen = getattr(getattr(nested_cls, "__dataclass_params__", None), "frozen", False)
                is_evented = any(
                    isinstance(c.__dict__.get("events"), SignalGroupDescriptor)
                    for c in nested_cls.mro()
                )
                if not is_frozen and not is_evented:
                    logger.warning(
                        f"{(root_cls or cls).__name__}: field '{cls.__name__}.{f.name}' is a "
                        f"non-evented, non-frozen dataclass ({nested_cls.__name__!r}). In-place "
                        f"mutations to its fields won't trigger sync — replace the whole object, "
                        f"use @dataclass(frozen=True), or add a SignalGroupDescriptor."
                    )
            valid_paths.update(
                _collect_valid_sync_paths(
                    nested_cls, skip, prefix=path, _visited=_visited,
                    root_cls=root_cls or cls, warn=warn,
                )
            )

    return valid_paths


# This is so that we only publish our own changes, not ones we just received. Keeps us from echo-ing other people's changes.
_local = threading.local()


@contextmanager
def _receiving():
    _local.receiving = True
    try:
        yield
    finally:
        _local.receiving = False


def _is_receiving() -> bool:
    return getattr(_local, "receiving", False)


class WeakMethodProxy:
    def __init__(self, method, callback=None):
        self._ref = weakref.WeakMethod(method, callback)

    def __call__(self, *args, **kwargs):
        method = self._ref()
        if method is None:
            raise ReferenceError("weakly-referenced object no longer exists")
        return method(*args, **kwargs)

    def __eq__(self, other):
        if isinstance(other, WeakMethodProxy):
            return self._ref == other._ref
        return NotImplemented

    def __hash__(self):
        return hash(self._ref)

@dataclass
class SyncableObject:
    events: ClassVar[SignalGroupDescriptor] = SignalGroupDescriptor()
    synq_topic: str
    synq_base_topic: str | None = None
    synq_session: Session | None = field(default_factory=current_session.get)
    synq_authoritive: bool = False  # Whether we're the "owner" of the object, or
    # a mirror.
    synq_lazy_publish: bool = False
    """Only publish changes if there are active subscribers on the zenoh network.
    Useful for reducing unnecessary network traffic when no one is listening."""
    synq_publish: bool = True
    """Whether this object should publish its changes to zenoh. Set to False to
    make this object receive-only."""
    synq_receive: bool = True
    """Whether this object should apply incoming changes from zenoh. Set to False
    to make this object publish-only."""
    synq_auto_start: bool = True
    """Automatically call sync() in __post_init__. Set to False if you need to
    configure the object before starting synchronization."""
    synq_check_receive_types = True
    """Validate that received values match the expected type annotation before
    applying them. Logs a warning and skips the update on type mismatch."""
    synq_signal_typeerror = Signal(zenoh.Query)
    synq_skip_rehydrate = set()
    synq_skip_sync = {
        "sync_lazy_publish",
        "synq_authoritive",
        "synq_session",
        "synq_publish",
        "synq_receive",
        "synq_auto_start",
        "synq_check_receive_types",
        "_synq_callbacks",
    }

    _synq_callbacks: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.synq_auto_start:
            self.sync()

    def sync(self):
        """Set up hookes for synchronization, including any relevent RPC hooks"""
        if not self.synq_session:
            raise Exception(f"No Session on {self.synq_topic}")
        if self.synq_authoritive and not self.synq_base_topic:
            self.synq_base_topic = self.synq_session.base_topic
        self.synq_session.register_type_recursive(type(self))
        type(self).valid_sync_paths()  # warm cache; emits warn_non_evented warnings once per class
        self.synq_session.objects[self.synq_absolute_path] = self

        if self.synq_authoritive:
            for name in dir(type(self)):
                value = getattr(type(self), name, None)
                if isinstance(value, RemoteMethod):
                    # Only bind and setup methods if we're authoritive
                    value.setup_zenoh_callback(self)

            tags = self.synq_type_tags()

            for tag in tags:
                tag = tag.removeprefix("!")
                self.sr_metadata.setup_zenoh_callback(
                    self, path=self.synq_absolute_path, name=f"sr_metadata/{tag}"
                )
        self.synq_publisher = self.synq_session.zenoh_session.declare_publisher(
            f"{self.synq_absolute_path}/**"
        )
        logger.trace(
            f"{self.synq_publisher} on {self.synq_session.zenoh_session.zid()}"
        )
        self.events.connect(self._zenoh_publish_changes)
        self.synq_subscriber = self.synq_session.zenoh_session.declare_subscriber(
            f"{self.synq_absolute_path}/**", WeakMethodProxy(self._zenoh_receive_changes)
        )
        logger.trace(
            f"{self.synq_subscriber} on {self.synq_session.zenoh_session.zid()}"
        )

    @staticmethod
    def _event_to_zenoh_path(event: EmissionInfo) -> str:
        parts = []
        for p in event.path:
            if not p.attr:
                raise Exception(
                    f"{event} non-attribute access. Did you use a list or dict?"
                )
            parts.append(p.attr)
        return "/".join(parts)

    @logger.catch()
    def _zenoh_publish_changes(self, event: EmissionInfo):
        """Publish changes from a remote zenoh object"""

        if _is_receiving():
            return
        if not self.synq_publish:
            return
        if self.synq_lazy_publish and (not self.synq_publisher or not self.synq_publisher.matching_status):
            logger.trace(f"{self} not matching subscribers, not publishing")
            return

        assert self.synq_session, "No session, can't publish changes"
        event_path = self._event_to_zenoh_path(event)

        if not event_path in self.valid_sync_paths():
            return

        value = event.args[0]
        full_path = f"{self.synq_absolute_path}/{event_path}"
        source_info = self.synq_session.source_info(event_path)

        codec = self.synq_session._encoder_for(value)
        if codec:
            payload, encoding = codec.encode(value)
            logger.trace(f"publishing (codec) {full_path} = {type(value).__name__}")
            self.synq_session.zenoh_session.put(
                full_path, payload, source_info=source_info, encoding=encoding,
            )
        else:
            enc_data = self.synq_session.type_registry.dumps(value)
            enc_data = enc_data.removesuffix("\n...")
            logger.trace(f"publishing yaml {full_path} = {enc_data}")
            self.synq_session.zenoh_session.put(
                full_path, enc_data, source_info=source_info,
                encoding=zenoh.Encoding.APPLICATION_YAML,
            )

    @logger.catch()
    def _zenoh_receive_changes(self, sample: zenoh.Sample):
        """Receive changes from a remote zenoh"""
        with _receiving():
            assert self.synq_session
            # This checks that we did not publish this ourselves.
            if (
                sample.source_info
                and sample.source_info.source_id.zid
                == self.synq_session.zenoh_session.zid()
            ):
                logger.trace(
                    f"Skipping update on {sample.key_expr}, same zenoh id {sample.source_info.source_id.zid}"
                )
                return
            if not self.synq_receive:
                return
            if not sample.payload:
                logger.warning(f"Update payload not ok")
                return

            assert self.synq_session, "No session"

            if not str(sample.key_expr).startswith(self.synq_absolute_path + "/"):
                logger.error(
                    f"Received '{sample.key_expr}' on {self.synq_absolute_path}"
                )
                return
            relative_path = str(sample.key_expr).removeprefix(
                self.synq_absolute_path + "/"
            )
            if relative_path not in self.valid_sync_paths():
                logger.warning(
                    f"Path {self.synq_absolute_path} -- {relative_path} not a valid path {self.valid_sync_paths()} "
                )
                return
            codec = self.synq_session._decoder_for(sample.encoding)
            if codec:
                obj = codec.decode(sample)
                logger.trace(f"received (codec) {self.synq_absolute_path}/{relative_path}")
            else:
                payload = sample.payload.to_string()
                logger.trace(f"received {self.synq_absolute_path} = {payload}")
                obj = self.synq_session.type_registry.load(payload)

            if not codec and self.synq_check_receive_types and not self.valid_sync_type(
                relative_path, obj
            ):
                logger.warning(
                    f"obj `{obj}` of type {type(obj)} at {sample.key_expr} is not of type {self._resolve_sync_type(relative_path)}"
                )

                return
            path = str(sample.key_expr).removeprefix(self.synq_absolute_path).split("/")
            logger.trace(f"received {path} = {obj}")
            # Convert "foo/bar/biz" → "root.foo.bar.biz" for DeepDiff path format
            deepdiff_path = "root." + ".".join(relative_path.split("/"))

            delta = Delta(
                flat_dict_list=[
                    {
                        "path": deepdiff_path,
                        "action": "values_changed",
                        "value": obj,
                    }
                ],
                mutate=True,  # mutates self in place rather than returning a copy
                raise_errors=True,
            )

            try:
                self + delta  # type: ignore[operator]  # mutate=True applies delta in-place
                logger.trace(f"applied {relative_path} = {obj}")
            except Exception as e:
                logger.error(f"Failed to apply delta for {relative_path}: {e}")

    @classmethod
    def from_topic(cls, topic, session=None):
        if not session:
            session = current_session.get()
        session.register_type_recursive(cls)
        new_obj = session.from_topic(topic)

        assert isinstance(new_obj, cls), f"{type(new_obj)} is not an instance of {cls}"
        return new_obj

    @property
    def synq_absolute_path(self) -> str:
        if self.synq_base_topic:
            return f"{self.synq_base_topic}/{self.synq_topic}"
        return f"{self.synq_topic}"

    @classmethod
    def synq_type_tags(cls) -> list:
        tags = []
        for c in cls.mro():
            class_tag = getattr(c, "yaml_tag", f"!{c.__name__}")
            if class_tag == "!object":
                break
            tags.append(class_tag)
        tags.sort()
        return tags

    @remote_method()
    def sr_metadata(self) -> SyncableObjectMetadata:
        """Returns topic path, YAML type tags, and authoritative node ID."""
        assert self.synq_session
        return {
            "topic": self.synq_absolute_path,
            "classes": self.synq_type_tags(),
            "authoritive_node": str(self.synq_session.zenoh_session.zid()),
        }

    @remote_method()
    def sr_rehydrate(self) -> Self:
        """Returns the full current state of this object."""
        return self

    @sr_rehydrate.client(raw=True)
    def sr_rehydrate_client(self, reply) -> Self:
        cls = self.__class__
        skip: set[str] = set()
        for c in cls.mro():
            if hasattr(c, "synq_skip_sync"):
                skip.update(c.synq_skip_sync)
            if hasattr(c, "synq_skip_rehydrate"):
                skip.update(c.synq_skip_rehydrate)
        syncable = {p for p in cls.valid_sync_paths() if "/" not in p}
        to_sync = list(syncable - skip)

        # Deserialize as a plain dict — catch-all multi-constructor ignores
        # the YAML tag and returns a mapping, avoiding a new zenoh session.
        plain_yaml = IsolatedYAML()
        plain_yaml.constructor.add_multi_constructor(
            '', lambda loader, _tag, node: loader.construct_mapping(node, deep=True)
        )
        assert reply.ok, f"RPC error: {reply.err.payload.to_string()}"
        updated = plain_yaml.load(reply.ok.payload.to_string())

        current = {f: getattr(self, f) for f in to_sync}
        diff = DeepDiff(current, updated, include_paths=to_sync)
        if not diff:
            return self

        delta = Delta(diff, mutate=True, raise_errors=True)
        current + delta  # type: ignore[operator]

        for f in to_sync:
            new_v = current[f]
            if new_v != getattr(self, f):
                self + Delta(  # type: ignore[operator]
                    flat_dict_list=[{"path": f"root.{f}", "action": "values_changed", "value": new_v}],
                    mutate=True,
                    raise_errors=True,
                )
        return self

    @remote_method()
    def sr_object_schema(self) -> dict:
        """Returns the JSON Schema for this object's syncable fields and RPC endpoints."""
        from SpiriSynq.schema import get_schema
        return get_schema(type(self))

    @classmethod
    def all_skip_rehydrate(cls) -> set:
        """Returns synq_skip_rehydrate on this and all parent classes merged"""
        result = set()
        for c in cls.mro():
            if hasattr(c, "synq_skip_rehydrate"):
                result.update(c.synq_skip_rehydrate)
        return result

    @classmethod
    def valid_sync_paths(cls) -> frozenset[str]:
        """
        Returns all valid Zenoh publish paths for this class.
        Merges synq_skip_sync across the MRO, then recursively walks
        dataclass field type annotations. Cached on the class itself.
        """
        cache_attr = "_synq_valid_paths_cache"
        if (cached := getattr(cls, cache_attr, None)) is not None:
            return cached

        skip = set()
        for c in cls.mro():
            if hasattr(c, "synq_skip_sync"):
                skip.update(c.synq_skip_sync)

        warn = next(
            (c.__dict__["warn_non_evented"] for c in cls.mro() if "warn_non_evented" in c.__dict__),
            True,
        )
        result = frozenset(_collect_valid_sync_paths(cls, skip, warn=warn))
        setattr(cls, cache_attr, result)
        return result

    @classmethod
    def _resolve_sync_type(cls, path: str) -> tuple[type, ...] | None:
        """
        Resolves the expected types at a given sync path. Cached on the class.
        Returns a tuple of valid types (for use with isinstance), or None if path is invalid.
        """
        cache_attr = "_synq_type_cache"
        cache: dict[str, tuple[type, ...] | None] | None = getattr(cls, cache_attr, None)
        if cache is None:
            cache = {}
            setattr(cls, cache_attr, cache)
        if path in cache:
            return cache[path]

        result = cls._resolve_sync_type_uncached(path)
        cache[path] = result
        return result

    @classmethod
    def _resolve_sync_type_uncached(cls, path: str) -> tuple[type, ...] | None:
        segments = path.strip("/").split("/")
        current_cls = cls

        for i, segment in enumerate(segments):
            if not dataclasses.is_dataclass(current_cls):
                return None

            hints = typing.get_type_hints(current_cls)
            if segment not in {f.name for f in dataclasses.fields(current_cls)}:
                return None

            annotation = hints.get(segment)
            if annotation is None:
                return None

            origin = get_origin(annotation)
            inner_types = (
                tuple(get_args(annotation)) if origin is Union else (annotation,)
            )

            if i == len(segments) - 1:
                return inner_types  # caller does isinstance(obj, inner_types)

            dc_types = [
                t
                for t in inner_types
                if isinstance(t, type) and dataclasses.is_dataclass(t)
            ]
            if not dc_types:
                return None
            current_cls = dc_types[0]

        return None

    @classmethod
    def valid_sync_type(cls, path: str, obj: object) -> bool:
        """Returns True if obj matches the expected type(s) at the given sync path."""
        inner_types = cls._resolve_sync_type(path)
        if inner_types is None:
            return False
        return isinstance(obj, inner_types)

    @classmethod
    def to_yaml(cls, representer, data):
        skip = cls.all_skip_rehydrate()
        # Reuse valid_sync_paths to get the top-level field names to serialize
        # (only direct fields, not nested paths)
        syncable = {p for p in cls.valid_sync_paths() if "/" not in p}
        field_names = {
            f.name
            for f in fields(cls)
            if f.name not in skip and f.name in syncable and not f.name.startswith("_")
        }
        yaml_tag = getattr(cls, "yaml_tag", f"!{cls.__name__}")
        return representer.represent_mapping(
            yaml_tag,
            {
                f.name: getattr(data, f.name)
                for f in fields(data)
                if f.name in field_names
            },
        )

    def sync_dumps(self) -> str:
        """Canonical yaml representation of this object"""
        assert self.synq_session
        return self.synq_session.type_registry.dumps(self)

    def __setstate__(self, state):
        # Routes through __init__ -> __post_init__ on yaml deserialize
        self.__init__(**state)

    def close(self):
        """Undeclare all zenoh resources and disconnect event handlers."""
        try:
            self.events.disconnect(self._zenoh_publish_changes)
        except Exception:
            pass

        sub = getattr(self, 'synq_subscriber', None)
        if sub is not None:
            try:
                sub.undeclare()
            except Exception:
                pass
            self.synq_subscriber = None

        pub = getattr(self, 'synq_publisher', None)
        if pub is not None:
            try:
                pub.undeclare()
            except Exception:
                pass
            self.synq_publisher = None

        for queryable in list(getattr(self, '_synq_callbacks', {}).values()):
            try:
                queryable.undeclare()
            except Exception:
                pass
        if hasattr(self, '_synq_callbacks'):
            self._synq_callbacks.clear()

    def __del__(self):
        try:
            logger.trace(f"Deleting object at {self.synq_absolute_path}")
        except AttributeError:
            pass
        self.close()