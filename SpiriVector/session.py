from dataclasses import dataclass, field, fields
from typing import Dict, ClassVar, Set, get_origin, get_type_hints, final
from SpiriVector.schema import get_schema
import zenoh
from ruamel.yaml import YAML
from psygnal import EmissionInfo, SignalGroupDescriptor, debounced
from psygnal.containers import EventedList, EventedDict, EventedSet
from collections import defaultdict
import time

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
    _registered_type_queryables: Dict[str, zenoh.Queryable] = field(default_factory=dict)  # NEW
    _in_zenoh_callback: bool = False

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
            query.reply(query.key_expr, payload=self.type_registry.dumps(schema))

        self._registered_type_queryables[type_name] = self.zenoh_session.declare_queryable(
            topic, handle_type_schema
        )
        logger.debug(f"Registered type schema queryable at {topic}")

    def setup_zenoh(self, path: str, authoratative=False):
        obj = self._synced_objects[path]
        assert obj

        def publish_attr_changes(event: EmissionInfo):
            if self._in_zenoh_callback:
                return
            attr_path = "/".join((e.attr.rstrip(".") for e in event.path))
            full_path = f"{path}/{attr_path}"
            value = event.args[0]
            value_str = self.type_registry.dumps(value)
            value_str = value_str.removesuffix("\n...")
            logger.debug(f"Publishing {full_path}:{value_str}")
            self.zenoh_session.put(full_path, value_str, congestion_control=zenoh.CongestionControl.DROP)

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
    warn_non_evented: ClassVar[bool] = True

    @final
    def sv_metadata(self):
        raise TypeError("sv_metadata is reserved by the zenoh transport layer.")

    @final
    def sv_schema(self):
        raise TypeError("sv_schema is reserved by the zenoh transport layer")

    def __init_subclass__(cls, warn_non_evented: bool = True, **kwargs):
        super().__init_subclass__(**kwargs)
        if not warn_non_evented or not hasattr(cls, "__dataclass_fields__"):
            return
        hints = get_type_hints(cls)
        reserved = cls.all_reserved_names()
        for f in fields(cls):
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

    @classmethod
    def all_reserved_names(cls):
        names = set()
        for klass in cls.__mro__:
            if "reserved_names" in klass.__dict__:
                val = klass.__dict__["reserved_names"]
                # Handle both sets/frozensets and single string values
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
        for f in fields(cls):
            if f.name in reserved_names:
                continue
            field_names.add(f.name)

        return representer.represent_mapping(
            cls.yaml_tag,
            {f.name: getattr(data, f.name) for f in fields(data) if f.name in field_names}
        )
    
    def __setstate__(self, state):
        # Routes through __init__ -> __post_init__ on yaml deserialize
        self.__init__(**state)
