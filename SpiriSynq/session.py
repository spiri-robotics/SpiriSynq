from dataclasses import dataclass, field, fields
from typing import Dict, ClassVar, Set, get_origin, get_type_hints, final, get_args
from SpiriSynq.schema import get_schema
import zenoh
from ruamel.yaml import YAML
from psygnal import EmissionInfo, SignalGroupDescriptor, debounced
from psygnal.containers import EventedList, EventedDict, EventedSet
from collections import defaultdict
from io import StringIO
import weakref

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
    _registered_type_queryables: Dict[str, zenoh.Queryable] = field(default_factory=dict)
    _in_zenoh_callback: bool = False
    raw_types: tuple[type, ...] = (bytes,) #Raw types get sent as a zenoh.Encoding.APPLICATION_OCTET_STREAM instead of being yaml serialized, this saves us the overhead of base64 encoding them.

    def __post_init__(self):
        self.zenoh_session = zenoh.open(self.config)
        self._synced_objects = weakref.WeakValueDictionary() #Path:obj mapping
        self._handlers_authoritative = defaultdict(set) #Authorative handler only run on one node. Include things like schema registration and metadata
        self._handlers_non_authoritative = defaultdict(set) #Non-authorative handlers synchronize attributes, every node runs them.
        self.setup_default_serializers()

    def setup_default_serializers(self):
        """Register YAML representers and constructors for evented container types."""
        from psygnal.containers import EventedList, EventedDict, EventedSet

        # Representers
        def represent_evented_list(dumper, data):
            # Use plain sequence representation with custom tag
            return dumper.represent_sequence('!EventedList', list(data), flow_style=None)
        def represent_evented_dict(dumper, data):
            return dumper.represent_mapping('!EventedDict', dict(data), flow_style=None)
        def represent_evented_set(dumper, data):
            # YAML set representation: mapping with each key -> null
            mapping = {item: None for item in data}
            return dumper.represent_mapping('!EventedSet', mapping, flow_style=None)

        self.type_registry.representer.add_representer(EventedList, represent_evented_list)
        self.type_registry.representer.add_representer(EventedDict, represent_evented_dict)
        self.type_registry.representer.add_representer(EventedSet, represent_evented_set)

        # Constructors
        def construct_evented_list(loader, node):
            data = loader.construct_sequence(node, deep=True)
            return EventedList(data)
        def construct_evented_dict(loader, node):
            data = loader.construct_mapping(node, deep=True)
            return EventedDict(data)
        def construct_evented_set(loader, node):
            # node is a mapping node
            mapping = loader.construct_mapping(node, deep=True)
            # keys are the set elements
            return EventedSet(mapping.keys())

        self.type_registry.constructor.add_constructor('!EventedList', construct_evented_list)
        self.type_registry.constructor.add_constructor('!EventedDict', construct_evented_dict)
        self.type_registry.constructor.add_constructor('!EventedSet', construct_evented_set)

    def cleanup_authoritative_handlers(self,path):
        logger.info(f"No longer authorative for {path}")
        for handler in self._handlers_authoritative[path]:
            handler.undeclare()

    def cleanup_non_authoritative_handlers(self,path):
        logger.info(f"No longer syncing {path}")
        for handler in self._handlers_non_authoritative[path]:
            handler.undeclare()

    def cleanup_all_handlers(self,path:str):
        self.cleanup_authoritative_handlers(path)
        self.cleanup_non_authoritative_handlers(path)

    def setup_authorative_handlers(self,obj: "SyncableObject", path:str):
        """Handlers that should only run on one node, like schema definition and metadata"""
        handlers = self._handlers_authoritative[path]

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

    def setup_non_authorative_handlers(self, obj: "SyncableObject", path:str, receive=True, publish=True):
        """Handlers that run on every node, like subscribers responsible for state changes"""

        def publish_attr_changes(event: EmissionInfo):
            if self._in_zenoh_callback:
                return
            # Build path parts and detect container index
            parts = []
            container_obj = obj  # start from the outer object
            container_path_parts = []
            found_index = False
            for e in event.path:
                # Determine the string representation of this path element
                if hasattr(e, 'attr') and e.attr is not None:
                    attr_raw = e.attr
                else:
                    attr_raw = str(e)
                # Remove leading/trailing dots that psygnal includes
                attr = attr_raw.rstrip('.').lstrip('.')
                # If we haven't found an index yet, navigate into container_obj
                if not found_index:
                    # Check if this part is an index (starts with '[')
                    if attr.startswith('['):
                        found_index = True
                        # Stop adding parts for the full path; we will publish at container_path_parts
                        # Do not include this index part
                        break
                    else:
                        # It's an attribute name
                        parts.append(attr)
                        container_path_parts.append(attr)
                        # Navigate into container_obj
                        container_obj = getattr(container_obj, attr)
                else:
                    # Already found index earlier, we can ignore further parts
                    pass
            if found_index:
                # Publish the whole container object (container_obj) at path up to container_path_parts
                attr_path = "/".join(container_path_parts)
                full_path = f"{path}/{attr_path}"
                value = container_obj
            else:
                # No index found, treat as regular attribute change
                attr_path = "/".join(parts)
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

        if publish:
            handler = self.zenoh_session.declare_publisher(f"{path}/**")
            self._handlers_non_authoritative[path].add(handler)
            self._handlers_non_authoritative[path].add(obj.events.connect(publish_attr_changes))

        reserved = type(obj).all_reserved_names()
        allowed_fields: set[str] = {
            f.name for f in fields(obj) if f.name not in reserved
        }

        def receive_attr_changes(sample = zenoh.Sample):
            _obj = weakref.proxy(obj)
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
            if sample.encoding == zenoh.Encoding.APPLICATION_OCTET_STREAM:
                value = sample.payload.to_bytes()
            else:
                raw = sample.payload.to_bytes().decode()
                value = self.type_registry.load(raw)    
            target = obj
            for part in parts[:-1]:
                target = getattr(target, part)

            # Apply the change while suppressing the re-publish guard
            self._in_zenoh_callback = True
            try:
                setattr(target, parts[-1], value)
            finally:
                self._in_zenoh_callback = False                        
        if receive:
            sub = self.zenoh_session.declare_subscriber(f"{path}/**", receive_attr_changes)
            self._handlers_non_authoritative[path].add(sub)


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

    def _register_type_recursive(self, cls: type) -> None:
        """Ensure cls and any SyncableObject types appearing in its fields are registered."""
        visited = set()
        def _register(t: type) -> None:
            # Only consider classes that are SyncableObject subclasses (or maybe dataclasses)
            if not isinstance(t, type):
                return
            if t in visited:
                return
            visited.add(t)
            # Check if it's a SyncableObject (has events classvar?)
            if not (hasattr(t, '__dataclass_fields__') and hasattr(t, 'events')):
                # Not a SyncableObject, skip
                return
            # Register this class if not already
            if not self.is_class_registered(t):
                self.type_registry.register_class(t)
                if not hasattr(t, "yaml_tag"):
                    t.yaml_tag = f"!{t.__name__}"
            # Process its fields
            try:
                hints = get_type_hints(t)
            except Exception:
                hints = {}
            for field_name, hint in hints.items():
                # Skip reserved fields
                if field_name in t.all_reserved_names():
                    continue
                origin = get_origin(hint)
                if origin is None:
                    # simple type
                    _register(hint)
                else:
                    # generic type, process each argument
                    for arg in get_args(hint):
                        if isinstance(arg, type):
                            _register(arg)
        _register(cls)

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


    def normalize_path(self, path: str) -> str:
        return "/".join((self.base_topic, path))

    def publish_synced_object(self, path: str, obj: "SyncableObject", authoritative=True, auto_register_type=True):
        if not auto_register_type:
            assert self.is_object_registered(obj)
        elif auto_register_type and not self.is_object_registered(obj):
            # recursively register the type and its dependencies
            self._register_type_recursive(type(obj))

        # Register type-level schema queryable whenever a type is first seen
        self.register_type_schema(type(obj))

        full_path = self.normalize_path(path)
        self._synced_objects[full_path] = obj
        if authoritative:
            self.setup_authorative_handlers(obj,full_path)
        self.setup_non_authorative_handlers(obj,full_path)
        return full_path

    def receive_synced_object(self, path: str, receive=True,publish=True) -> "SyncableObject":
        receiver = self.zenoh_session.get(path)
        reply = receiver.recv()
        assert reply.ok
        data = reply.ok.payload
        obj = self.type_registry.load(StringIO(bytes(data).decode("utf-8")))

        self.setup_non_authorative_handlers(obj, path, receive=receive, publish=publish)

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
    reserved_names = {"events", "sv_metadata", "sv_schema"}
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
