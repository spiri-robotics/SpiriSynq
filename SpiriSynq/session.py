import zenoh
import socket
import os
from deepdiff import DeepDiff, Delta
from dataclasses import dataclass, field
from typing import get_type_hints, get_origin, get_args
from SpiriSynq.remote_callables import rpc_call
from SpiriSynq.shutdown import register_session
from SpiriSynq.codecs import Codec, BUILTIN_CODECS
from SpiriSynq.serializer import SessionSerializer
from loguru import logger
import weakref
from contextvars import ContextVar
from contextlib import contextmanager
from collections import defaultdict
from psygnal.containers import EventedList, EventedDict, EventedSet


def _represent_evented_list(representer, data):
    return representer.represent_sequence("!EventedList", list(data))


def _construct_evented_list(constructor, node):
    return EventedList(constructor.construct_sequence(node, deep=True))


def _represent_evented_dict(representer, data):
    return representer.represent_mapping("!EventedDict", dict(data))


def _construct_evented_dict(constructor, node):
    return EventedDict(constructor.construct_mapping(node, deep=True))


def _represent_evented_set(representer, data):
    return representer.represent_sequence("!EventedSet", list(data))


def _construct_evented_set(constructor, node):
    return EventedSet(constructor.construct_sequence(node, deep=True))

def _default_base_topic() -> str:
    return os.getenv("SPIRI_SYNQ_BASE_TOPIC", socket.gethostname())


@dataclass
class Session:
    """A connection to the Zenoh network.

    Most applications use the module-level default session created at import time
    and never construct a ``Session`` directly. You only need to create one explicitly
    if you want to connect with custom Zenoh config, or to connect to multiple
    independent Zenoh networks from the same process.

    The session opens a Zenoh peer connection on construction and closes it on
    garbage collection. For deterministic cleanup, call :meth:`close` explicitly.

    Example — custom config::

        import zenoh
        from SpiriSynq.session import Session

        config = zenoh.Config.from_file("zenoh.json5")
        session = Session(config=config)
        counter = Counter("myapp/counter", synq_session=session, synq_authoritive=True)

    Example — multiple sessions::

        session_a = Session()
        session_b = Session()

        with session_b.as_default():
            # Objects created here use session_b
            mirror = Counter.from_topic("myhost/myapp/counter")
    """

    config: zenoh.Config = field(default_factory=zenoh.Config)
    """Zenoh configuration. Defaults to peer-mode auto-discovery with no router."""
    base_topic: str = field(default_factory=_default_base_topic)
    """Topic prefix prepended to all authoritative objects on this session.
    Defaults to the hostname, or the ``SPIRI_SYNQ_BASE_TOPIC`` environment variable if set."""
    type_registry: SessionSerializer = field(default_factory=SessionSerializer)
    """YAML serialiser/deserialiser used for all payloads on this session.
    Types are registered here by :meth:`register_type_recursive`."""
    zenoh_session: zenoh.Session = field(init=False)
    """The underlying Zenoh session. Available after construction."""
    objects: weakref.WeakValueDictionary = field(
        default_factory=weakref.WeakValueDictionary
    )
    """Weak map of ``absolute_path → SyncableObject`` for all live objects on this session."""
    _sequince_number_for_path: dict[str,int] = field(default_factory=lambda: defaultdict(lambda:0))
    _codecs: list = field(default_factory=list, repr=False)
    """Custom codecs registered via :meth:`register_codec`."""

    @contextmanager
    def as_default(self: "Session"):
        """Context manager that makes this session the default for the current thread/task.

        Any ``SyncableObject`` constructed inside the block that does not pass
        ``synq_session=`` explicitly will use this session instead of the
        module-level default.

        Can also be used as a plain context manager around a block of code::

            session2 = Session()
            with session2.as_default():
                mirror = Counter.from_topic("myhost/myapp/counter")
                # mirror was created on session2
        """
        token = current_session.set(self)
        try:
            yield self
        finally:
            current_session.reset(token)

    def is_class_registered(self, cls):
        """Return True if *cls* has been registered with this session's type registry."""
        representer_registered = cls in self.type_registry.representer.yaml_representers
        tag = getattr(cls, "yaml_tag", None)
        constructor_registered = (
            tag in self.type_registry.constructor.yaml_constructors if tag else False
        )
        return representer_registered and constructor_registered

    def from_topic(self, topic):
        """Fetch and return the current state of an object at *topic* via ``sr_rehydrate``.

        The type must already be registered with this session (call
        :meth:`register_type_recursive` first, or use ``SyncableObject.from_topic``
        which handles registration automatically).

        Returns the deserialised object. The concrete type depends on the YAML tag
        in the reply, so the return type is ``Any``.
        """
        new_obj = rpc_call(f"{topic}/sr_rehydrate", self)
        return new_obj

    def list_topics(self, type_filter: str = "", prefix: str = ""):
        """Yield metadata dicts for all objects currently discoverable on the network.

        Queries ``**/sr_metadata/<type_filter>`` and yields one dict per reply.
        Each dict contains at minimum ``topic``, ``classes``, and ``authoritive_node``.

        Args:
            type_filter: If given, only topics whose class list includes this type
                name (without the ``!`` YAML tag prefix) are returned.
            prefix: Restrict the search to topics under this key prefix.

        Example::

            for meta in session.list_topics(type_filter="Counter"):
                print(meta["topic"])

        Works identically to ``synq topic list`` on the CLI.
        """
        query_topic = (
            f"{prefix}/**/sr_metadata/{type_filter}"
            if prefix
            else f"**/sr_metadata/{type_filter}"
        )
        query_topic = query_topic.strip("/").removesuffix("/")

        replies = self.zenoh_session.get(
            query_topic, consolidation=zenoh.ConsolidationMode.NONE
        )
        for reply in replies:
            if reply.ok:
                raw = reply.ok.payload.to_bytes().decode("utf-8")
                # parse YAML into dict
                metadata = self.type_registry.load(raw)
                logger.debug(f"Found topic: {metadata}")
                yield metadata
            else:
                assert reply.err, "Reply not OK and no reply err. Weird." #Begone type checker
                logger.warning(
                    f"Error reply in list_topics: {reply.err.payload.to_string()}"
                )

    def register_type_recursive(self, cls: type) -> None:
        """Register *cls* and every type reachable from its field annotations.

        Walks all type hints transitively. For each type:
        - Already representable (via MRO against the YAML representer) → skip.
        - Otherwise → assign a yaml_tag if missing, call register_class, then
          verify it took. Raises TypeError if a type cannot be registered so
          the problem surfaces at sync() time rather than at first serialisation.
        """
        visited: set[type] = set()

        def _is_representable(t: type) -> bool:
            rep = self.type_registry.representer
            return t in rep.yaml_representers

        def _register(t: type) -> None:
            if not isinstance(t, type) or t in visited:
                return
            visited.add(t)

            if not _is_representable(t):
                if not hasattr(t, "yaml_tag"):
                    t.yaml_tag = f"!{t.__name__}"
                self.type_registry.register_class(t)
                if not _is_representable(t):
                    raise TypeError(
                        f"{t.__qualname__!r} cannot be YAML-serialized. "
                        f"Add to_yaml / from_yaml class methods or register a "
                        f"custom representer/constructor before calling sync()."
                    )

            try:
                hints = get_type_hints(t)
            except Exception:
                hints = {}

            skip: set[str] = set()
            for c in getattr(t, "__mro__", [t]):
                for attr in ("synq_skip_sync", "synq_skip_rehydrate"):
                    if attr in c.__dict__:
                        skip.update(c.__dict__[attr])

            for field_name, hint in hints.items():
                if field_name in skip:
                    continue
                _register_annotation(hint)

        def _register_annotation(annotation) -> None:
            origin = get_origin(annotation)
            if origin is None:
                if isinstance(annotation, type):
                    _register(annotation)
            else:
                for arg in get_args(annotation):
                    if isinstance(arg, type):
                        _register(arg)

        _register(cls)

    def register_codec(self, codec: "Codec") -> None:
        """Register a custom :class:`Codec` on this session.

        The encoder is selected by matching ``codec.python_type`` against the
        Python type of the value being published (MRO walk, most-specific first).
        The decoder is selected by matching ``codec.zenoh_schema`` against the
        encoding reported by an incoming zenoh sample.

        Example::

            import numpy as np
            import zenoh
            from SpiriSynq.session import Codec, current_session

            session = current_session.get()
            session.register_codec(Codec(
                python_type=np.ndarray,
                zenoh_schema=zenoh.Encoding("image/jpeg"),
                encoder=lambda arr: (encode_jpeg(arr), zenoh.Encoding("image/jpeg")),
                decoder=lambda sample: decode_jpeg(sample.payload.to_bytes()),
            ))
        """
        self._codecs.append(codec)

    def _encoder_for(self, value) -> "Codec | None":
        """Return the first registered codec whose python_type matches type(value) via MRO."""
        for mro_type in type(value).__mro__:
            for codec in self._codecs:
                if codec.python_type is mro_type:
                    return codec
        return None

    def _decoder_for(self, encoding) -> "Codec | None":
        """Return the first registered codec whose zenoh_schema matches *encoding*."""
        for codec in self._codecs:
            if codec.zenoh_schema == encoding:
                return codec
        return None

    def source_info(self, path:str):
        #We keep track of per path sequince numbers.
        source_info = zenoh.SourceInfo(
            source_id=self.zenoh_session.id,
            source_sn=self._sequince_number_for_path[path],
        )
        self._sequince_number_for_path[path]+=1
        return source_info

    def close(self):
        try:
            self.zenoh_session.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    def __post_init__(self):
        self._codecs.extend(BUILTIN_CODECS)
        self.type_registry.representer.add_representer(EventedList, _represent_evented_list)
        self.type_registry.representer.add_representer(EventedDict, _represent_evented_dict)
        self.type_registry.representer.add_representer(EventedSet, _represent_evented_set)
        self.type_registry.constructor.add_constructor("!EventedList", _construct_evented_list)
        self.type_registry.constructor.add_constructor("!EventedDict", _construct_evented_dict)
        self.type_registry.constructor.add_constructor("!EventedSet", _construct_evented_set)
        self.zenoh_session = zenoh.open(self.config)
        logger.info(f"Started zenoh session {self.zenoh_session.zid()}")

        # Register for deterministic shutdown. This does NOT keep the session
        # alive (the registry is weak); it only lets the shutdown hook find and
        # close it before the interpreter joins zenoh's non-daemon threads. See
        # SpiriSynq/shutdown.py for why this, and not __del__ / atexit / context
        # managers, is what works.
        register_session(self)


current_session: ContextVar[Session] = ContextVar("current_session", default=Session())
