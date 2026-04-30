import zenoh
import socket
import os
from ruamel.yaml import YAML
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.representer import SafeRepresenter
from deepdiff import DeepDiff, Delta
from dataclasses import dataclass, field, fields
from typing import ClassVar, get_type_hints, get_origin, get_args
from SpiriSynq.remote_callables import RemoteMethod, rpc_call, remote_method
from psygnal import EmissionInfo, SignalGroupDescriptor
from loguru import logger
import inspect
import weakref
from contextvars import ContextVar
from contextlib import contextmanager

base_path = os.getenv("SPIRI_SYNQ_BASE_TOPIC",socket.gethostname())

def make_isolated_yaml() -> YAML:
    """Create a YAML instance with its own isolated constructor/representer state.

    Work around for https://sourceforge.net/p/ruamel-yaml/tickets/341/

    This isn't super nessesary, but has some minor security implications for
    republishers. Since crossing the boundary between two SynqSessions is
    generally security critical, we'll be extra careful here.
    """

    class IsolatedConstructor(SafeConstructor):
        yaml_constructors = SafeConstructor.yaml_constructors.copy()

    class IsolatedRepresenter(SafeRepresenter):
        yaml_representers = SafeRepresenter.yaml_representers.copy()

    y = YAML(typ=["string","safe"])
    y.Constructor = IsolatedConstructor
    y.Representer = IsolatedRepresenter
    return y

@contextmanager
def with_session(session: "Session"):
    """
    Sets the default session for newly created objects.
    """
    token = current_session.set(session)
    try:
        yield session
    finally:
        current_session.reset(token)

@dataclass
class Session:
    config: zenoh.Config = field(default_factory=zenoh.Config)
    base_topic: str = base_path
    type_registry: YAML = field(default_factory=make_isolated_yaml)
    zenoh_session: zenoh.Session = field(init=False)
    objects: weakref.WeakValueDictionary = field(default_factory=weakref.WeakValueDictionary)

    def is_class_registered(self, cls):
        representer_registered = cls in self.type_registry.representer.yaml_representers
        tag = getattr(cls, 'yaml_tag', None)
        constructor_registered = (
            tag in self.type_registry.constructor.yaml_constructors
            if tag else False
        )
        return representer_registered and constructor_registered
    
    def from_topic(self,topic):
        """Return an arbitrary object from a remote topic. Note that
        this object could be of any type, and you must have registered
        the type in advance.
        """
        new_obj = rpc_call(f"{topic}/sr_rehydrate",self)
        return new_obj    


    def list_topics(self, type_filter: str = "", prefix: str = ""):
        """Yield topic metadata dicts for discovered topics.

        Works like the CLI `topic list` command.
        """
        query_topic = f"{prefix}/**/sr_metadata/{type_filter}" if prefix else f"**/sr_metadata/{type_filter}"
        query_topic = query_topic.strip("/").removesuffix("/")

        replies = self.zenoh_session.get(query_topic, consolidation=zenoh.ConsolidationMode.NONE)
        for reply in replies:
            if reply.ok:
                raw = reply.ok.payload.to_bytes().decode("utf-8")
                # parse YAML into dict
                metadata = self.type_registry.load(raw)
                logger.debug(f"Found topic: {metadata}")
                yield metadata
            else:
                logger.warning(f"Error reply in list_topics: {reply.err.payload.to_string()}")

    def register_type_recursive(self, cls: type) -> None:
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
                    if field_name in t.all_skip_rehydrate():
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

    def __post_init__(self):
        self.zenoh_session = zenoh.open(self.config)    

current_session: ContextVar[Session] = ContextVar('current_session', default=Session())

@dataclass
class SyncableObject:
    events: ClassVar[SignalGroupDescriptor] = SignalGroupDescriptor()
    topic: str
    base_topic: str|None = None
    session: Session|None = field(default_factory=current_session.get)
    authoritive: bool = False #Whether we're the "owner" of the object, or
    # a mirror.
    skip_rehydrate = set()
    skip_sync = {"events", "session", "authoritive", "absolute_path"}

    def __post_init__(self):
        if not self.session:
            return
        if self.authoritive and not self.base_topic:
            self.base_topic = self.session.base_topic
        logger.debug(f"Registering {self.type_tags()} for {self.absolute_path}")
        self.session.register_type_recursive(type(self))
        self.session.objects[self.absolute_path] = self

        # FIXED: Iterate class, not instance — avoids triggering __get__ on descriptors
        for name in dir(type(self)):
            value = getattr(type(self), name, None)
            if isinstance(value, RemoteMethod):
                # Only bind and setup if we're authoritive
                if self.authoritive:
                    value.setup_zenoh_callback(self)

        tags = self.type_tags()
        logger.debug(f"{tags}")

        for tag in tags:
            tag = tag.removeprefix("!")
            self.sr_metadata.setup_zenoh_callback(self,path=self.absolute_path,name=f"sr_metadata/{tag}")

    def _zenoh_publish_changes(self, event: EmissionInfo):
        """Publish changes from a remote zenoh object"""
        assert self.session, "No session, can't publish changes"
        # new_value = event
        pass
    def _zenoh_receive_changes(self,query:zenoh.Query):
        """Receive changes from a remote zenoh"""
        pass

    @classmethod
    def from_topic(cls,topic,session=None):
        if not session:
            session = current_session.get()
        session.register_type_recursive(cls)
        new_obj = rpc_call(f"{topic}/sr_rehydrate",session)
        assert isinstance(new_obj,cls), f"{type(new_obj)} is not an instance of {cls}"
        return new_obj

    @property
    def absolute_path(self) -> str:
        return f"{self.base_topic}/{self.topic}".removeprefix("/")

    @classmethod
    def type_tags(cls) -> list:
        tags = []
        for c in cls.mro():
            class_tag = getattr(c,"yaml_tag",f"!{c.__name__}")
            if class_tag == '!object': break
            tags.append(class_tag)
        tags.sort()
        return tags
    
    @remote_method
    def sr_metadata(self):
        return  {'topic': self.absolute_path, "classes": self.type_tags(),"node": str(self.session.zenoh_session.zid())}

    @remote_method
    def sr_rehydrate(self):
        return self

    @classmethod
    def all_skip_rehydrate(cls) -> set:
        """Returns skip rehyrdate on this and all parent classes merged"""
        result = set()
        for c in cls.mro():
            if hasattr(c, 'skip_rehydrate'):
                result.update(c.skip_rehydrate)
        return result
    
    @classmethod
    def all_skip_sync(cls) -> set:
        """Returns skip sync on this and all parent classes merged"""
        result = set()
        for c in cls.mro():
            if hasattr(c, 'skip_sync'):
                result.update(c.skip_sync)
        return result    

    def dumps(self):
        return self.session.type_registry.dumps(self)

    @classmethod
    def to_yaml(cls, representer, data):
        # Only serialize actual dataclass fields, nothing set in __post_init__
        field_names = set()
        reserved_names = set()
        reserved_names.update(cls.all_skip_rehydrate())
        reserved_names.update(cls.all_skip_sync())
        for f in fields(cls):
            if f.name in reserved_names:
                continue
            if f.name.startswith("_"):
                continue
            field_names.add(f.name)

        yaml_tag = getattr(cls, "yaml_tag", f"!{cls.__name__}")
        return representer.represent_mapping(
            yaml_tag,
            {f.name: getattr(data, f.name) for f in fields(data) if f.name in field_names}
        )
    
    def __setstate__(self, state):
        # Routes through __init__ -> __post_init__ on yaml deserialize
        self.__init__(**state)

    def __del__(self):
        logger.debug(f"Cleaning up {self.absolute_path}")