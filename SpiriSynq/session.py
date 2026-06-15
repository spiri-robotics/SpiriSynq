import zenoh
import socket
import os
from ruamel.yaml import YAML
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.representer import SafeRepresenter
from deepdiff import DeepDiff, Delta
from dataclasses import dataclass, field
from typing import get_type_hints, get_origin, get_args, Any
from SpiriSynq.remote_callables import rpc_call
from SpiriSynq.shutdown import register_session
from loguru import logger
import weakref
from contextvars import ContextVar
from contextlib import contextmanager
import io
from collections import defaultdict

def _default_base_topic() -> str:
    return os.getenv("SPIRI_SYNQ_BASE_TOPIC", socket.gethostname())


class IsolatedYAML(YAML):
    """YAML subclass with isolated constructor/representer state and safe string dumping.

    Works around:
    - https://sourceforge.net/p/ruamel-yaml/tickets/341/ (isolated state)
    - https://sourceforge.net/p/ruamel-yaml/tickets/367/ (thread safety)
    - https://sourceforge.net/p/ruamel-yaml/tickets/272/ (emitter state poisoning)

    Not sure if 367 or 272 are the real problem here, but this should deal with both.
    """

    def __init__(self, *args, **kwargs):
        # Force typ to avoid 'string' plugin which causes emitter issues with bytes
        if 'typ' not in kwargs:
            kwargs['typ'] = 'safe'
        super().__init__(*args, **kwargs)

        # Isolate constructor and representer state
        class IsolatedConstructor(SafeConstructor):
            yaml_constructors = SafeConstructor.yaml_constructors.copy()

        class IsolatedRepresenter(SafeRepresenter):
            yaml_representers = SafeRepresenter.yaml_representers.copy()

        self.Constructor = IsolatedConstructor
        self.Representer = IsolatedRepresenter

    def dumps(self, data):
        """Serialize data to a YAML string."""
        buf = io.StringIO()
        # Must be set before __enter__: YAMLContextManager.__init__ captures
        # self._output from the YAML instance.
        self._output = buf
        # Use the context-manager protocol instead of dump(data, stream).
        # A failed dump (e.g. RepresenterError for an unregistered type) leaves
        # _context_manager/_output/_emitter/_serializer in a half-finished state;
        # on the next plain dump() call, ruamel sees _context_manager is non-None,
        # takes the context-manager branch, and raises AttributeError because
        # _output hasn't been re-initialised yet.  __exit__ unconditionally calls
        # teardown_output(), which resets all of that state even when the body raises.
        with self:
            self.dump(data)
        return buf.getvalue().removesuffix("...\n").removesuffix("\n")


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
    type_registry: IsolatedYAML = field(default_factory=IsolatedYAML)
    """YAML serialiser/deserialiser used for all payloads on this session.
    Types are registered here by :meth:`register_type_recursive`."""
    zenoh_session: zenoh.Session = field(init=False)
    """The underlying Zenoh session. Available after construction."""
    objects: weakref.WeakValueDictionary = field(
        default_factory=weakref.WeakValueDictionary
    )
    """Weak map of ``absolute_path → SyncableObject`` for all live objects on this session."""
    _sequince_number_for_path: dict[str,int] = field(default_factory=lambda: defaultdict(lambda:0))

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
                assert reply.err, "Reply not OK and no reply err. Weird."
                logger.warning(
                    f"Error reply in list_topics: {reply.err.payload.to_string()}"
                )

    def register_type_recursive(self, cls: type) -> None:  # cls: type[SyncableObject]
        """Register *cls* and all dataclass types needed to construct it.

        SpiriSynq uses YAML tags (``!ClassName``) to round-trip typed objects.
        A type must be registered before its tag can be deserialised. This method
        walks the full annotation tree of *cls* transitively and registers every
        dataclass it encounters — including plain frozen dataclasses used as field
        values — so you only need to call it on the root type.

        *cls* must be a ``SyncableObject`` subclass.
        """
        visited = set()

        def _register(t: type) -> None:
            if t in visited:
                return
            visited.add(t)
            if not hasattr(t, "__dataclass_fields__"):
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
            skip = t.all_skip_rehydrate() if hasattr(t, "all_skip_rehydrate") else set()
            for field_name, hint in hints.items():
                if field_name in skip:
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
        self.zenoh_session = zenoh.open(self.config)
        logger.info(f"Started zenoh session {self.zenoh_session.zid()}")

        # Register for deterministic shutdown. This does NOT keep the session
        # alive (the registry is weak); it only lets the shutdown hook find and
        # close it before the interpreter joins zenoh's non-daemon threads. See
        # SpiriSynq/shutdown.py for why this, and not __del__ / atexit / context
        # managers, is what works.
        register_session(self)


current_session: ContextVar[Session] = ContextVar("current_session", default=Session())
