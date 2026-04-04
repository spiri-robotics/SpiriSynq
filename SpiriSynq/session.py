from dataclasses import dataclass, field, fields
from typing import Dict, ClassVar, Set, get_origin, get_type_hints, final
from SpiriSynq.schema import get_schema
import zenoh
from ruamel.yaml import YAML
from psygnal import EmissionInfo, SignalGroupDescriptor, debounced
from psygnal.containers import EventedList, EventedDict, EventedSet
from collections import defaultdict

import socket
from loguru import logger

hostname = socket.gethostname()

PLAIN_CONTAINER_TYPES = (list, dict, set)
EVENTED_CONTAINER_TYPES = (EventedList, EventedDict, EventedSet)

@dataclass
class Session:
    config: zenoh.Config = field(default_factory=zenoh.Config)
    base_topic: str = hostname
    type_registry: YAML = field(default_factory=lambda: YAML(typ=["safe", "string"]))
    _synced_objects: Dict[str, "SyncableObject"] = field(default_factory=dict)
    _synced_object_links: Dict[int, Set[zenoh.Publisher]] = field(default_factory=lambda: defaultdict(set))
    _registered_type_queryables: Dict[str, zenoh.Queryable] = field(default_factory=dict)
    _in_zenoh_callback: bool = False
    raw_types: tuple[type, ...] = (bytes,) #Raw types get sent as a zenoh.Encoding.APPLICATION_OCTET_STREAM instead of being yaml serialized, this saves us the overhead of base64 encoding them.

    def __post_init__(self):
        self.zenoh_session = zenoh.open(self.config)

    def is_class_registered(self, cls):
        representer_registered = cls in self.type_registry.representer.yaml_representers
        tag = getattr(cls, 'yaml_tag', None)
        constructor_registered = (
            tag in self.type_registry.constructor.yaml_constructors
            if tag else False
        )
        return representer_registered and constructor_registered

    def is_object_registered(self, obj):
        return self.is_class_registered(type(obj))

    def register_type_schema(self, cls):
        """Declare a queryable for <base_topic>/sv_type_schema/<type_name> if not already registered."""
        type_name = getattr(cls, 'yaml_tag', f"!{cls.__name__}").removeprefix("!")
        topic = f"{self.base_topic}/sv_type_schema/{type_name}"

        if type_name in self._registered_type_queryables:
            return  # Already registered

        def handle_type_schema(query: zenoh.Query):
            schema = get_schema(cls)
            schema["x-node-path"]=self.base_topic
            query.reply(query.key_expr, payload=self.type_registry.dumps(schema))

        self._registered_type_queryables[type_name] = self.zenoh_session.declare_queryable(
            topic, handle_type_schema
        )
        logger.info(f"Registered type schema queryable at {topic}")

    def setup_zenoh(self, path: str, authoratative=False):
        obj = self._synced_objects[path]
        assert obj

        def publish_attr_changes(event: EmissionInfo):
            if self._in_zenoh_callback:
                return
            attr_path = "/".join((e.attr.rstrip(".") for e in event.path))
            full_path = f"{path}/{attr_path}"
            value = event.args[0]

            if isinstance(value, self.raw_types):
                payload = bytes(value)
                encoding = zenoh.Encoding.APPLICATION_OCTET_STREAM
                logger.debug(f"Publishing {full_path} (binary, {len(payload)} bytes)")
            else:
                value_str = self.type_registry.dumps(value).removesuffix("\n...")
                payload = value_str.encode()
                encoding = zenoh.Encoding.TEXT_PLAIN
                logger.debug(f"Publishing {full_path}:{value_str}")

            self.zenoh_session.put(
                full_path,
                payload,
                encoding=encoding,
                congestion_control=zenoh.CongestionControl.DROP,
            )

        self.zenoh_session.declare_publisher(f"{path}/**")
        obj.events.connect(publish_attr_changes)

        if authoratative:
            handlers = self._synced_object_links[id(obj)]

            def handle_rehydrate_request(query: zenoh.Query):
                query.reply(query.key_expr, payload=self.type_registry.dumps(obj))

            handlers.add(self.zenoh_session.declare_queryable(path, handle_rehydrate_request))

            type_name = obj.yaml_tag.removeprefix("!")

            def handle_metadata(query: zenoh.Query):
                metadata = {'path': path, "type": type_name}
                query.reply(query.key_expr, payload=self.type_registry.dumps(metadata))

            handlers.add(self.zenoh_session.declare_queryable(path + "/sv_metadata", handle_metadata))
            handlers.add(self.zenoh_session.declare_queryable(path + f"/sv_metadata/{type_name}", handle_metadata))

            def handle_schema(query: zenoh.Query):
                schema = get_schema(type(obj))
                schema["x-sv-path"] = path
                query.reply(query.key_expr, payload=self.type_registry.dumps(schema))

            handlers.add(self.zenoh_session.declare_queryable(path + "/sv_object_schema", handle_schema))

    def normalize_path(self, path: str) -> str:
        return "/".join((self.base_topic, path))

    def publish_synced_object(self, path: str, obj: "SyncableObject", authoratative=True, auto_register_type=True):
        if not auto_register_type:
            assert self.is_object_registered(obj)
        elif auto_register_type and not self.is_object_registered(obj):
            self.type_registry.register_class(type(obj))
            if not hasattr(type(obj), "yaml_tag"):
                type(obj).yaml_tag = f"!{type(obj).__name__}"

        # Register type-level schema queryable whenever a type is first seen
        self.register_type_schema(type(obj))  # NEW

        full_path = self.normalize_path(path)
        self._synced_objects[full_path] = obj
        self.setup_zenoh(full_path, authoratative=authoratative)
        return full_path

    def receive_synced_object(self, path: str, receive_only=False):
        receiver = self.zenoh_session.get(path)
        reply = receiver.recv()
        assert reply.ok
        data = reply.ok.payload
        obj = self.type_registry.load(data)

        if not receive_only:
            self.publish_synced_object(path, obj, auto_register_type=False, authoratative=False)

        # Build the set of fields we are allowed to touch:
        # dataclass fields that are NOT in the class's reserved_names.
        reserved = type(obj).all_reserved_names()
        allowed_fields: set[str] = {
            f.name for f in fields(obj) if f.name not in reserved
        }

        def on_change(sample: zenoh.Sample):
            key_str = str(sample.key_expr)

            # Derive the relative attribute path from the full zenoh key
            if not key_str.startswith(path + "/"):
                return
            rel_path = key_str[len(path) + 1:]   # e.g. "speed" or "pose/position/x"
            parts = rel_path.split("/")
            top_field = parts[0]

            # Guard: only registered (dataclass), non-reserved fields
            if top_field not in allowed_fields:
                logger.debug(f"Skipping unregistered/reserved field update: {top_field!r}")
                return

            # Decode the incoming value — mirrors publish_attr_changes encoding
            if sample.encoding == zenoh.Encoding.APPLICATION_OCTET_STREAM:
                value = sample.payload.to_bytes()
            else:
                raw = sample.payload.to_bytes().decode().removesuffix("\n...")
                value = self.type_registry.load(raw)

            # Navigate to the parent object for nested paths
            target = obj
            for part in parts[:-1]:
                target = getattr(target, part)

            # Apply the change while suppressing the re-publish guard
            self._in_zenoh_callback = True
            try:
                setattr(target, parts[-1], value)
            finally:
                self._in_zenoh_callback = False

        sub = self.zenoh_session.declare_subscriber(f"{path}/**", on_change)
        self._synced_object_links[id(obj)].add(sub)

        return obj


@dataclass
class SyncableObject:
    """A dataclass that can be synced over the zenoh network
    Will only sync fields set as part of the class, no attributes
    created in __post_init__ or via other means.

    Attributes in reserved_names are not synced.

    Container objects (dicts, lists, sets) will have much worse on-wire performance than
    regular attributes, and should be avoided. This is because we
    need to send the entire state of a container object to the other side
    every time it changes. If you find yourself reaching for a container,
    you may want to consider directly publishing to zenoh instead.

    All container attributes should be of a SyncableObject type.

    You can suppress syncable container warnings with the `warn_non_evented` class variable.

    from psygnal.containers import EventedList, EventedDict, EventedSet
    class MyObject(SyncableObject, warn_non_evented=False):
        unsyncableList: List = field(default_factory=list)
        syncableList: EventedList = field(default_factory=EventedList)

    """
    events: ClassVar[SignalGroupDescriptor] = SignalGroupDescriptor()
    reserved_names = {"events", "sv_metadata"}
    skip_rehydrate = set()  # Attrs that should not be included in initial rehydration
    warn_non_evented: ClassVar[bool] = True
    _checked_classes: ClassVar[set] = set()  # Track already-warned classes

    @final
    def sv_metadata(self):
        raise TypeError("sv_metadata is reserved by the zenoh transport layer.")

    @final
    def sv_schema(self):
        raise TypeError("sv_schema is reserved by the zenoh transport layer")

    def __post_init__(self):
        cls = type(self)
        if cls not in SyncableObject._checked_classes:
            SyncableObject._checked_classes.add(cls)
            _check_syncable_fields(cls)

    def __init_subclass__(cls, warn_non_evented: bool = True, **kwargs):
        super().__init_subclass__(**kwargs)
        # Store the per-class warn setting; field checking is deferred to first
        # instantiation via __post_init__ so that @dataclass has fully processed
        # the class before we call fields().
        cls.warn_non_evented = warn_non_evented

    @classmethod
    def all_reserved_names(cls):
        names = set()
        for klass in cls.__mro__:
            if "reserved_names" in klass.__dict__:
                val = klass.__dict__["reserved_names"]
                if isinstance(val, (set, frozenset, list, tuple)):
                    names.update(val)
                else:
                    names.add(val)
        return names

    @classmethod
    def all_skip_rehydrate_names(cls):
        names = set()
        for klass in cls.__mro__:
            if "skip_rehydrate" in klass.__dict__:
                val = klass.__dict__["skip_rehydrate"]
                if isinstance(val, (set, frozenset, list, tuple)):
                    names.update(val)
                else:
                    names.add(val)
        return names

    @classmethod
    def to_yaml(cls, representer, data):
        # Only serialize actual dataclass fields, nothing set in __post_init__
        field_names = set()
        reserved_names = cls.all_reserved_names()
        skip_names = cls.all_skip_rehydrate_names()
        for f in fields(cls):
            if f.name in reserved_names or f.name in skip_names:
                continue
            field_names.add(f.name)

        return representer.represent_mapping(
            cls.yaml_tag,
            {f.name: getattr(data, f.name) for f in fields(data) if f.name in field_names}
        )

    def __setstate__(self, state):
        # Routes through __init__ -> __post_init__ on yaml deserialize
        self.__init__(**state)


def _check_syncable_fields(cls: type) -> None:
    """Check a fully-decorated dataclass for non-evented container or dataclass fields
    and emit warnings. Called once per class on first instantiation, after @dataclass
    has finished processing the class."""
    if not getattr(cls, "warn_non_evented", True):
        return
    try:
        cls_fields = fields(cls)
    except TypeError:
        return

    hints = get_type_hints(cls)
    reserved = cls.all_reserved_names()

    for f in cls_fields:
        if f.name in reserved:
            continue
        tp = get_origin(hints.get(f.name)) or hints.get(f.name)
        if not isinstance(tp, type):
            continue
        if hasattr(tp, "__dataclass_fields__") and not hasattr(tp, "events"):
            logger.warning(
                f"{cls.__name__}.{f.name}: '{tp.__name__}' is a non-evented dataclass. "
                f"Field changes will not emit signals or be synchronized."
            )
        elif issubclass(tp, PLAIN_CONTAINER_TYPES) and not issubclass(tp, EVENTED_CONTAINER_TYPES):
            logger.warning(
                f"{cls.__name__}.{f.name}: '{tp.__name__}' is a non-evented container. "
                f"Mutations will not emit signals or be synchronized."
            )
