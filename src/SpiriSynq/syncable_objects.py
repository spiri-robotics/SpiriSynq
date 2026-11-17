"""`SyncableObject`: a dataclass kept in sync across processes, and `SubSyncableDataclass`.

An authoritative object (`synq_authoritive=True`) owns its topic: it publishes its fields,
answers remote calls, and serves its state and schema. A mirror (`Cls.from_topic(topic)`)
follows it. The core does the networking; this module turns Python attribute changes into the
core's field values and back.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import threading
import types
import typing
import weakref
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from loguru import logger
from psygnal import EmissionInfo, Signal, SignalGroupDescriptor

from SpiriSynq import _core, qos, values
from SpiriSynq.callbacks import OnChange
from SpiriSynq.link import Link, ObjectNotFound
from SpiriSynq.remote_callables import YAML, RemoteMethod
from SpiriSynq.schema import get_schema, members
from SpiriSynq.session import Session, current_session
from SpiriSynq.values import BadValue, from_value, hints_of, to_value, wire_fields
from SpiriSynq.wasm import WasmFunction, call_args

BYTES = "zenoh/bytes"


class Sample:
    """A field's value on the wire, for its `<field>_before_send` and `<field>_after_receive`.

    Arriving, it has `payload` (bytes), `encoding` and `attachment`, and `decode()` decodes it
    the usual way. Going out, `send(payload, encoding=..., attachment=...)` sends bytes as they
    are, and `send_value(value)` sends a value the usual way: exactly one of them, once.
    """

    def __init__(self, payload: bytes = b"", encoding: str = BYTES, attachment=None, hint=Any):
        self.payload = payload
        self.encoding = encoding
        self.attachment = attachment
        self._hint = hint
        self._send = None

    def decode(self) -> Any:
        """The value, decoded the usual way."""
        if self.encoding != YAML:
            raise ValueError(f"a {self.encoding} sample isn't encoded the usual way")
        return from_value(_core.yaml_load(self.payload), self._hint)

    def send(self, payload: bytes, *, encoding: str = BYTES, attachment: bytes | None = None):
        """Send the value as these bytes."""
        if self._send is None:
            raise RuntimeError("only a before_send can send its sample, once")
        send, self._send = self._send, None
        send(_core.Sample(bytes(payload), encoding, attachment))

    def send_value(self, value: Any) -> None:
        """Send a value the usual way."""
        self.send(_core.yaml_dump(to_value(value)), encoding=YAML)


@dataclass
class SubSyncableDataclass:
    """A dataclass synced field by field inside a syncable object: `robot.gps.latitude = 45.0`
    publishes `<topic>/gps/latitude` alone. Its own class carries its fields' settings."""

    events: ClassVar[SignalGroupDescriptor] = SignalGroupDescriptor()
    synq_skip_sync: ClassVar[set] = set()
    synq_skip_rehydrate: ClassVar[set] = set()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        values.register(cls)


# --- what the core is told about each field -----------------------------------------------


@dataclass
class _Field:
    path: str  # attribute path: `gps/latitude`
    names: tuple[str, ...]
    hint: Any
    owner: type  # the class declaring the field, which holds its settings
    before_send: Any = None  # Python hooks; WASM ones are the core's
    after_receive: Any = None
    hooked: bool = False  # any hook, Python or WASM: the core keeps no value for it

    def options(self) -> _core.FieldOptions:
        name, cls = self.names[-1], self.owner
        priority = cls.__dict__.get(f"{name}_qos", qos.data)
        reliability = cls.__dict__.get(f"{name}_reliability", qos.drop)
        if not isinstance(priority, qos.Priority):
            raise TypeError(f"{cls.__qualname__}.{name}_qos must be a qos preset, like qos.data")
        if not isinstance(reliability, qos.Reliability):
            raise TypeError(f"{cls.__qualname__}.{name}_reliability must be qos.drop or reliable")
        return _core.FieldOptions(
            priority=priority.name,
            reliable=reliability.reliable,
            ignore_incoming=bool(cls.__dict__.get(f"{name}_ignore_incoming", False)),
            lazy=bool(cls.__dict__.get(f"{name}_lazy", False)),
            raw=self.after_receive is not None,
        )


def _sub_syncable(hint) -> type | None:
    """The `SubSyncableDataclass` a field holds: its annotation, or a branch of its union."""
    if isinstance(hint, type) and issubclass(hint, SubSyncableDataclass):
        return hint
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        for arg in typing.get_args(hint):
            if isinstance(arg, type) and issubclass(arg, SubSyncableDataclass):
                return arg
    return None


def _hook(cls: type, name: str, kind: str):
    """A field's Python hook `<name>_<kind>` on `cls`, or `None`; `True` for a WASM one."""
    for klass in cls.__mro__:
        if (hook := klass.__dict__.get(f"{name}_{kind}")) is not None:
            return True if isinstance(hook, WasmFunction) else hook
    return None


def _plan(cls: type) -> dict[str, _Field]:
    cached = cls.__dict__.get("_synq_plan")
    if cached is not None:
        return cached
    plan: dict[str, _Field] = {}

    def walk(klass, fields, prefix, names, seen):
        hints = hints_of(klass)
        for f in fields:
            path = f"{prefix}{f.name}"
            hint = hints.get(f.name, f.type)
            before, after = (
                _hook(klass, f.name, "before_send"),
                _hook(klass, f.name, "after_receive"),
            )
            plan[path] = _Field(
                path=path,
                names=(*names, f.name),
                hint=hint,
                owner=klass,
                before_send=before if callable(before) and before is not True else None,
                after_receive=after if callable(after) and after is not True else None,
                hooked=before is not None or after is not None,
            )
            sub = _sub_syncable(hint)
            if sub is not None and sub not in seen:
                walk(sub, wire_fields(sub), f"{path}/", (*names, f.name), seen | {sub})

    walk(cls, cls.synq_fields(), "", (), frozenset())
    cls._synq_plan = plan
    return plan


# --- how this thread is applying a change ---------------------------------------------------

NORMAL, LOCAL, QUIET = "normal", "local", "quiet"
_state = threading.local()


def _mode() -> str:
    return getattr(_state, "mode", NORMAL)


@contextlib.contextmanager
def _applying(mode: str):
    """Within the block, attribute changes on this thread are applied as `mode` says:
    `NORMAL` publishes them, `LOCAL` changes a mirror's copy only, `QUIET` tells the core
    nothing (it already knows)."""
    old = _mode()
    _state.mode = mode
    try:
        yield
    finally:
        _state.mode = old


def _running() -> set:
    if not hasattr(_state, "running"):
        _state.running = set()
    return _state.running


# --- the object ---------------------------------------------------------------------------


@dataclass
class SyncableObject:
    """A dataclass whose fields are kept in sync over the network. See the docs, "Objects and
    fields"."""

    events: ClassVar[SignalGroupDescriptor] = SignalGroupDescriptor()

    synq_topic: str
    synq_authoritive: bool = field(default=False, kw_only=True)
    """Whether this process owns the object, or mirrors it."""
    synq_session: Session | None = field(default=None, kw_only=True, repr=False, compare=False)
    synq_base_topic: str | None = field(default=None, kw_only=True, repr=False, compare=False)
    synq_follow_links: bool = field(default=True, kw_only=True, repr=False, compare=False)
    """On a mirror: whether link fields hold mirrors of their targets, or `Link`s."""
    synq_auto_start: bool = field(default=True, kw_only=True, repr=False, compare=False)
    """Start syncing when created. Off, call `sync()` once the object is ready."""

    synq_skip_sync: ClassVar[set] = set()
    """Fields never published or received."""
    synq_skip_rehydrate: ClassVar[set] = set()
    """Fields synced as they change, but left out of the state a new mirror fetches."""
    synq_type_id: ClassVar[str | None] = None
    """`<uuid>:<version>`, for a class that isn't in a package on PyPI."""

    synq_signal_bad_data = Signal(str, str)
    """`(path, reason)`: an incoming value couldn't be applied, and was dropped."""
    synq_signal_alive = Signal(bool)
    """On a mirror: the owner went away (`False`) or came back (`True`)."""
    synq_signal_listeners = Signal(str, bool)
    """On an owner: whether anyone listens to a field changed. `(path, listening)`."""

    def __post_init__(self):
        if self.synq_auto_start:
            self.sync()

    # --- the type -----------------------------------------------------------------------------

    @classmethod
    def synq_fields(cls) -> list[dataclasses.Field]:
        """The dataclass fields that are synced."""
        return [f for f in wire_fields(cls) if not f.name.startswith("synq_")]

    @classmethod
    def synq_key_of(cls, path: str) -> str:
        """The key, under the topic, of the field at attribute path `path`."""
        return path

    @classmethod
    def synq_schema(cls) -> dict:
        """The type's JSON Schema, as the core and other processes see it."""
        return get_schema(cls)

    @classmethod
    def _synq_core_schema(cls) -> _core.Schema:
        cached = cls.__dict__.get("_synq_checked")
        if cached is None:
            try:
                cached = _core.Schema(get_schema(cls))
            except _core.SynqError as e:
                raise TypeError(f"{cls.__qualname__}: {e}") from None
            cls._synq_checked = cached
        return cached

    @classmethod
    def _synq_member_map(cls) -> tuple[dict[str, RemoteMethod], list[OnChange]]:
        cached = cls.__dict__.get("_synq_member_cache")
        if cached is None:
            methods, callbacks = {}, []
            for _, member in members(cls):
                if isinstance(member, RemoteMethod):
                    methods.setdefault(member.name, member)
                elif isinstance(member, OnChange) and not isinstance(member.func, WasmFunction):
                    callbacks.append(member)
            cached = (methods, callbacks)
            cls._synq_member_cache = cached
        return cached

    # --- starting -----------------------------------------------------------------------------

    def sync(self) -> None:
        """Start syncing: open the object in the core, as its owner or as a mirror."""
        cls = type(self)
        session = self.synq_session or current_session.get()
        object.__setattr__(self, "synq_session", session)
        topic = self.synq_topic
        relative = topic.startswith("./")
        if relative:
            object.__setattr__(self, "synq_topic", topic.removeprefix("./"))
        if (relative or self.synq_authoritive) and not self.synq_base_topic:
            object.__setattr__(self, "synq_base_topic", session.base_topic)
        path = session.resolve_topic(self.synq_absolute_path)
        schema = cls._synq_core_schema()
        plan = _plan(cls)
        options = {cls.synq_key_of(p): f.options() for p, f in plan.items()}
        self._synq_ready = threading.Event()
        self._synq_links: dict[str, SyncableObject] = {}
        self._synq_linked_from: tuple[weakref.ref, str] | None = None
        node = session.node
        if self.synq_authoritive:
            methods, _ = cls._synq_member_map()
            raw = [name for name, m in methods.items() if m.raw]
            object_id = session._register(
                lambda: node.create_owner(path, schema, options, raw), self
            )
        else:
            object_id = session._register(lambda: node.create_mirror(path, schema, options), self)
        self._synq_id = object_id
        if self.synq_authoritive:
            for f in cls.synq_fields():
                value = getattr(self, f.name)
                if plan[f.name].before_send is None or value is not None:
                    self._synq_send(f.name, value)
            self._synq_ready.set()
        self.events.connect(self._synq_on_event)

    @classmethod
    def from_topic(
        cls,
        topic: str,
        session: Session | None = None,
        *,
        timeout: float | None = None,
        wait: bool = True,
        follow_links: bool = True,
    ) -> Self:
        """A mirror of the object on `topic`.

        Waits up to `timeout` seconds (the session's call timeout by default) for the owner's
        state, and raises `TimeoutError` if it doesn't come. With `wait=False`, returns at once:
        the state arrives when the owner answers.
        """
        session = session or current_session.get()
        obj = cls.__new__(cls)
        for f in dataclasses.fields(cls):
            if f.default is not dataclasses.MISSING:
                value = f.default
            elif f.default_factory is not dataclasses.MISSING:
                value = f.default_factory()
            else:
                value = None
            object.__setattr__(obj, f.name, value)
        object.__setattr__(obj, "synq_topic", topic)
        object.__setattr__(obj, "synq_session", session)
        object.__setattr__(obj, "synq_authoritive", False)
        object.__setattr__(obj, "synq_follow_links", follow_links)
        obj.sync()
        if wait and not session.wait(obj._synq_ready, timeout or session.call_timeout):
            obj.close()
            raise TimeoutError(f"nobody served {topic} in time")
        return obj

    # --- state --------------------------------------------------------------------------------

    @property
    def synq_absolute_path(self) -> str:
        """The topic this object actually uses."""
        if self.synq_base_topic:
            return f"{self.synq_base_topic}/{self.synq_topic}"
        return self.synq_topic

    @property
    def synq_alive(self) -> bool:
        """Whether the owner is alive: always, on the owner itself."""
        if self.synq_authoritive:
            return True
        return self.synq_session.node.alive(self._synq_id)

    def synq_has_listeners(self, path: str | None = None) -> bool:
        """On an owner: whether any other process listens to a field, or to any field."""
        key = None if path is None else type(self).synq_key_of(path)
        return self.synq_session.node.has_listeners(self._synq_id, key)

    def synq_rehydrate(self, timeout: float | None = None) -> None:
        """On a mirror: fetch the owner's full state again, and wait for it."""
        self._synq_ready.clear()
        self.synq_session.node.rehydrate(self._synq_id)
        if not self.synq_session.wait(self._synq_ready, timeout or self.synq_session.call_timeout):
            raise TimeoutError(f"nobody served {self.synq_absolute_path} in time")

    def synq_call(self, method: str, timeout: float | None = None, **args) -> list:
        """Call a remote method by name, with arguments by name: every value it replies with.
        For callers without the class, such as the CLI."""
        session = self.synq_session
        values_ = {k: to_value(v) for k, v in args.items()}
        timeout = timeout or session.call_timeout
        request = session.node.call(
            self._synq_id, method, values_, raw_replies=True, timeout_ms=int(timeout * 1000)
        )
        from SpiriSynq.remote_callables import RpcException

        out = []
        for value, error in session.replies(request, timeout + 5):
            if error is not None:
                raise RpcException(error[0], error[1], topic=self.synq_absolute_path)
            if isinstance(value, _core.Sample):
                if value.encoding != YAML:
                    out.append(value.payload)
                    continue
                value = _core.yaml_load(value.payload)
            out.append(values._untyped(value))
        return out

    def close(self) -> None:
        """Release the object's network resources. An owner's mirrors see it go away."""
        object_id = getattr(self, "_synq_id", None)
        if object_id is None:
            return
        self._synq_id = None
        with contextlib.suppress(Exception):
            self.events.disconnect(self._synq_on_event)
        self.synq_session._forget(object_id)

    def __del__(self):
        with contextlib.suppress(Exception):
            self.close()

    def __str__(self) -> str:
        shown = ", ".join(f"{f.name}={getattr(self, f.name)!r}" for f in self.synq_fields())
        name, path = type(self).__name__, self.synq_absolute_path
        return f"{name}({path!r}, {shown})" if shown else f"{name}({path!r})"

    __hash__ = object.__hash__

    # --- paths --------------------------------------------------------------------------------

    def _synq_get(self, path: str):
        obj = self
        for name in path.split("/"):
            if obj is None:
                return None
            obj = getattr(obj, name)
        return obj

    def _synq_set(self, path: str, value) -> bool:
        """Set the attribute at `path`; `False` if a dataclass holding it is `None`."""
        *parents, name = path.split("/")
        obj = self
        for parent in parents:
            obj = getattr(obj, parent)
            if obj is None:
                return False
        setattr(obj, name, value)
        return True

    # --- going out ----------------------------------------------------------------------------

    def _synq_on_event(self, info: EmissionInfo) -> None:
        steps = info.path
        if any(not step.attr for step in steps):
            path = steps[0].attr  # a container changed in place: send it whole
        else:
            path = "/".join(step.attr for step in steps)
        plan = _plan(type(self))
        if path not in plan or getattr(self, "_synq_id", None) is None:
            return
        self._synq_changed(path, self._synq_get(path))

    def _synq_changed(self, path: str, value) -> None:
        mode = _mode()
        if mode == NORMAL:
            self._synq_send(path, value)
        elif mode == LOCAL and not _plan(type(self))[path].hooked:
            self.synq_session.node.set_field_local(
                self._synq_id, type(self).synq_key_of(path), to_value(value)
            )
        if self.synq_authoritive:
            self._synq_callbacks("owner", path, value)

    def _synq_send(self, path: str, value) -> None:
        cls = type(self)
        node, key = self.synq_session.node, cls.synq_key_of(path)
        before = _plan(cls)[path].before_send
        if before is None:
            node.set_field(self._synq_id, key, to_value(value))
            return
        sample = Sample()
        sample._send = lambda s: node.send_raw(self._synq_id, key, s)
        holder = self._synq_get(path.rpartition("/")[0]) if "/" in path else self
        before(holder, value, sample)
        if sample._send is not None:
            raise RuntimeError(f"{path}_before_send returned without sending")

    def _synq_callbacks(self, side: str, path: str, value) -> None:
        _, callbacks = type(self)._synq_member_map()
        for callback in callbacks:
            if callback.side != side or path not in callback.fields:
                continue
            running = _running()
            token = (id(self), id(callback))
            if token in running:
                continue  # setting the watched field in its own callback doesn't run it again
            running.add(token)
            try:
                with _applying(NORMAL if side == "owner" else LOCAL):
                    callback.run(self, path, value)
            finally:
                running.discard(token)

    # --- coming in ----------------------------------------------------------------------------

    def _synq_notice(self, notice) -> None:
        if isinstance(notice, _core.Incoming):
            self._synq_incoming(notice)
        elif isinstance(notice, _core.BadData):
            self._synq_bad(notice.path, notice.reason)
        elif isinstance(notice, _core.Rehydrated):
            self._synq_rehydrated(notice.complete)
        elif isinstance(notice, _core.Alive):
            self.synq_signal_alive.emit(notice.alive)
        elif isinstance(notice, _core.Listeners):
            self.synq_signal_listeners.emit(notice.path, notice.listening)

    def _synq_bad(self, path: str, reason: str) -> None:
        logger.warning(f"{self.synq_absolute_path}: dropped a value for {path}: {reason}")
        self.synq_signal_bad_data.emit(path, reason)

    def _synq_path_of(self, key: str) -> str:
        return key

    def _synq_incoming(self, notice: _core.Incoming) -> None:
        path = self._synq_path_of(notice.path)
        f = _plan(type(self)).get(path)
        if f is None:
            return
        data = notice.data
        wire = None
        try:
            if isinstance(data, _core.Sample):
                # Only a field with a Python after_receive gets its samples as they are.
                holder = self._synq_get(path.rpartition("/")[0]) if "/" in path else self
                sample = Sample(data.payload, data.encoding, data.attachment, f.hint)
                value = from_value(to_value(f.after_receive(holder, sample)), f.hint)
            else:
                wire = data
                value = from_value(wire, f.hint)
        except Exception as e:
            self._synq_bad(path, str(e))
            return
        if self.synq_authoritive:
            with _applying(NORMAL):
                if not self._synq_set(path, value):
                    self._synq_bad(path, "the dataclass holding it is None")
            return
        if wire is not None and not f.hooked:
            try:
                changed = self.synq_session.node.set_field_local(self._synq_id, notice.path, wire)
            except _core.SynqError as e:
                self._synq_bad(path, str(e))
                return
            if not changed and self._synq_same(path, wire):
                return
        self._synq_apply(path, value)
        self._synq_callbacks("mirror", path, value)

    def _synq_same(self, path: str, wire) -> bool:
        try:
            return to_value(self._synq_get(path)) == wire
        except TypeError:
            return False

    def _synq_apply(self, path: str, value) -> None:
        """Set a mirror's copy of a field to what the core already holds."""
        f = _plan(type(self))[path]
        if self.synq_follow_links:
            value = self._synq_follow(path, value, f.hint)
        with _applying(QUIET):
            if not self._synq_set(path, value):
                self._synq_bad(path, "the dataclass holding it is None")
        if any(e.get("role") == "on_change_mirror" for e in self._synq_wasm().values()):
            self._synq_refresh()

    def _synq_wasm(self) -> dict:
        return get_schema(type(self)).get("x-wasm", {})

    def _synq_refresh(self) -> None:
        """Bring the attributes in line with the core's copy, after WASM code set fields."""
        cls = type(self)
        plan = _plan(cls)
        node = self.synq_session.node
        for f in cls.synq_fields():
            if plan[f.name].hooked:
                continue
            wire = node.field(self._synq_id, cls.synq_key_of(f.name))
            if wire is None or self._synq_same(f.name, wire):
                continue
            try:
                value = from_value(wire, plan[f.name].hint)
            except BadValue as e:
                self._synq_bad(f.name, str(e))
                continue
            if self.synq_follow_links and not self.synq_authoritive:
                value = self._synq_follow(f.name, value, plan[f.name].hint)
            with _applying(QUIET):
                setattr(self, f.name, value)

    def _synq_rehydrated(self, complete: bool) -> None:
        if complete:
            self._synq_ready.set()
            return
        linked = self._synq_linked_from
        if linked is None or self.synq_alive:
            return
        parent = linked[0]()
        if parent is not None:
            parent._synq_not_found(linked[1], self)

    # --- links --------------------------------------------------------------------------------

    def _synq_follow(self, path: str, value, hint):
        """`value` with each `Link` in it replaced by a mirror of its target."""
        if isinstance(value, Link) and not isinstance(value, ObjectNotFound):
            target = _link_class(hint)
            if target is None:
                return value
            return self._synq_mirror_of(path, value.topic, target)
        if isinstance(value, list):
            item = (typing.get_args(hint) or (Any,))[0]
            return [self._synq_follow(path, v, item) for v in value]
        if isinstance(value, dict):
            args = typing.get_args(hint)
            item = args[1] if len(args) > 1 else Any
            return {k: self._synq_follow(path, v, item) for k, v in value.items()}
        return value

    def _synq_mirror_of(self, path: str, topic: str, cls: type) -> SyncableObject:
        mirror = self._synq_links.get(topic)
        if mirror is None or not isinstance(mirror, cls) or mirror._synq_id is None:
            mirror = cls.from_topic(topic, session=self.synq_session, wait=False)
            mirror._synq_linked_from = (weakref.ref(self), path)
            self._synq_links[topic] = mirror
        return mirror

    def _synq_not_found(self, path: str, mirror: SyncableObject) -> None:
        """A followed link's target isn't served: put a placeholder where its mirror was."""
        topic = mirror.synq_absolute_path
        if self._synq_links.get(topic) is not mirror:
            return
        del self._synq_links[topic]
        placeholder = ObjectNotFound(topic)
        cls = type(mirror)
        mirror.close()

        def rebind(timeout):
            found = cls.from_topic(topic, session=self.synq_session, timeout=timeout)
            found._synq_linked_from = (weakref.ref(self), path)
            self._synq_links[topic] = found
            self._synq_replace(path, placeholder, found)
            return found

        object.__setattr__(placeholder, "_rebind", rebind)
        self._synq_replace(path, mirror, placeholder)

    def _synq_replace(self, path: str, old, new) -> None:
        def swap(value):
            if value is old:
                return new
            if isinstance(value, list):
                return [swap(v) for v in value]
            if isinstance(value, dict):
                return {k: swap(v) for k, v in value.items()}
            return value

        with _applying(QUIET):
            self._synq_set(path, swap(self._synq_get(path)))

    # --- WASM ---------------------------------------------------------------------------------

    def _synq_run_wasm(self, code: WasmFunction, args: tuple, kwargs: dict):
        given = {k: to_value(v) for k, v in call_args(code.func, args, kwargs).items()}
        try:
            result = self.synq_session.node.run_wasm(self._synq_id, code.name, given)
        except _core.SynqError as e:
            raise RuntimeError(str(e)) from None
        self._synq_refresh()
        return from_value(result, hints_of(code.func).get("return", Any))

    async def _synq_run_wasm_async(self, code: WasmFunction, args: tuple, kwargs: dict):
        return await asyncio.to_thread(self._synq_run_wasm, code, args, kwargs)

    # --- serving calls ------------------------------------------------------------------------

    def _synq_serve(self, call: _core.Call) -> None:
        methods, _ = type(self)._synq_member_map()
        method = methods.get(call.method)
        if method is None:
            self.synq_session.node.reply_error(call.query, "LookupError", "no such method")
            return
        method.serve(self, call)


def _link_class(hint) -> type | None:
    """The syncable class a link field's annotation names."""
    if isinstance(hint, type) and issubclass(hint, SyncableObject):
        return hint
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        for arg in typing.get_args(hint):
            if isinstance(arg, type) and issubclass(arg, SyncableObject):
                return arg
    return None


@functools.cache
def _untyped_class(type_id: str, names: tuple[str, ...]) -> type:
    fields = [(n, Any, field(default=None)) for n in names]
    name = type_id.rsplit(".", 1)[-1].rsplit(":", 1)[-1] or "Untyped"
    cls = dataclasses.make_dataclass(name if name.isidentifier() else "Untyped", fields,
                                     bases=(SyncableObject,))  # fmt: skip
    return cls


def untyped_class(schema: dict) -> type:
    """A syncable class made from a served schema: its fields, typed `Any`."""
    names = tuple(
        n for n in schema.get("properties", {}) if n.isidentifier() and not n.startswith("synq_")
    )
    cls = _untyped_class(schema["$id"], names)
    cls._synq_schema = schema
    return cls
