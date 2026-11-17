"""`Session`: one connection to the zenoh network, with the SpiriSynq core on it.

Most programs use the default session, `current_session.get()`, opened on first use. Open your
own for a custom zenoh configuration, or to join several networks from one process.

Threads: the core's notices come out on a *pump* thread, which hands replies to the calls
waiting for them, and everything else to the *callback* thread, in order. Field changes,
signals and callbacks run there: SpiriSynq's network thread. Remote methods called on an owner
run on a pool of worker threads, so a slow one doesn't hold up the others.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import json
import os
import queue
import socket
import threading
import time
import weakref
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from SpiriSynq import _core
from SpiriSynq.link import check_topic

if TYPE_CHECKING:
    from SpiriSynq.syncable_objects import SyncableObject

_REQUEST_NOTICES = (_core.Reply, _core.RequestEnded, _core.Discovered, _core.SchemaFetched)


def _default_base_topic() -> str:
    if (override := os.getenv("SPIRI_SYNQ_BASE_TOPIC")) is not None:
        return override
    # The host's /etc/hostname, bind-mounted here into containers: a container's own
    # hostname is per container, not per machine.
    try:
        if name := Path("/etc/spirisynq_base_topic").read_text().strip():
            return name
    except OSError:
        pass
    return socket.gethostname()


class Session:
    """A connection to the zenoh network.

    `config` is zenoh's configuration, as a dict or JSON5 text; by default, zenoh's own
    defaults (peer mode, finding peers by multicast). `call_timeout` is how long, in seconds, a
    remote call waits when its method doesn't say.
    """

    def __init__(
        self,
        config: dict | str | None = None,
        *,
        base_topic: str | None = None,
        call_timeout: float = 10.0,
        wasm_timeout: float = 1.0,
        wasm_memory_mib: int = 64,
    ):
        if config is None:
            config = {}
        if isinstance(config, dict):
            config = json.dumps(config)
        self.base_topic = base_topic if base_topic is not None else _default_base_topic()
        self.call_timeout = call_timeout
        self.node = _core.Node(config, wasm_timeout=wasm_timeout, wasm_memory_mib=wasm_memory_mib)
        self._lock = threading.RLock()
        self._objects: weakref.WeakValueDictionary[_core.ObjectId, SyncableObject] = (
            weakref.WeakValueDictionary()
        )
        self._requests: dict[_core.RequestId, queue.SimpleQueue] = {}
        self._callbacks: queue.SimpleQueue = queue.SimpleQueue()
        self._closed = threading.Event()
        self._workers = concurrent.futures.ThreadPoolExecutor(32, "SpiriSynq call")
        self._pump = threading.Thread(target=self._run_pump, name="SpiriSynq pump", daemon=True)
        self._callback_thread = threading.Thread(
            target=self._run_callbacks, name="SpiriSynq network", daemon=True
        )
        self._pump.start()
        self._callback_thread.start()
        _live.add(self)
        logger.debug(f"opened session {self.node.zid}")

    @property
    def zid(self) -> str:
        """This session's zenoh id."""
        return self.node.zid

    @contextlib.contextmanager
    def as_default(self):
        """Make this the session objects use when they aren't given one, inside the block."""
        token = current_session.set(self)
        try:
            yield self
        finally:
            current_session.reset(token)

    def resolve_topic(self, topic: str) -> str:
        """`topic` with a leading `.` chunk replaced by the base topic, checked.

        `./camera` is `<base_topic>/camera`. `ValueError` if the result isn't a topic.
        """
        chunks = topic.split("/")
        if chunks[0] == ".":
            chunks[0] = self.base_topic
        resolved = "/".join(chunks)
        try:
            return check_topic(resolved)
        except ValueError:
            raise ValueError(
                f"Invalid topic {topic!r}: '.' is only allowed as the first chunk, and a topic "
                f"has no empty chunks, '..', or any of '*$?#'"
            ) from None

    # --- objects --------------------------------------------------------------------------

    def _register(self, open_object, obj: SyncableObject) -> _core.ObjectId:
        """Open an object in the core with `open_object()`, and route its notices to `obj`."""
        with self._lock:
            object_id = open_object()
            self._objects[object_id] = obj
        return object_id

    def _object(self, object_id: _core.ObjectId) -> SyncableObject | None:
        with self._lock:
            return self._objects.get(object_id)

    def _forget(self, object_id: _core.ObjectId) -> None:
        with self._lock:
            self._objects.pop(object_id, None)
        with contextlib.suppress(_core.SynqError):
            self.node.close_object(object_id)

    # --- requests -------------------------------------------------------------------------

    def _request_queue(self, request: _core.RequestId) -> queue.SimpleQueue:
        with self._lock:
            q = self._requests.get(request)
            if q is None:
                q = self._requests[request] = queue.SimpleQueue()
            return q

    def _take(self, request, deadline: float | None):
        q = self._request_queue(request)
        while True:
            wait = 0.1 if deadline is None else min(0.1, deadline - time.monotonic())
            if wait <= 0:
                with self._lock:
                    self._requests.pop(request, None)
                raise TimeoutError("no reply in time")
            try:
                return q.get(timeout=wait)
            except queue.Empty:
                if self._closed.is_set():
                    raise RuntimeError("the session is closed") from None

    def notices(self, request, timeout: float | None) -> Iterator:
        """A request's notices, until it ends."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            notice = self._take(request, deadline)
            if isinstance(notice, _core.RequestEnded):
                with self._lock:
                    self._requests.pop(request, None)
                if notice.end == "cut_short":
                    raise TimeoutError("the call was cut short: no answer in time")
                return
            yield notice

    def replies(self, request, timeout: float | None) -> Iterator[tuple[Any, Any]]:
        """A call's replies as `(value, error)`, until it ends. `TimeoutError` if it's cut
        short."""
        for notice in self.notices(request, timeout):
            yield notice.value, notice.error

    async def areplies(self, request, timeout: float | None) -> AsyncIterator[tuple[Any, Any]]:
        """`replies`, for async code."""
        replies = self.replies(request, timeout)
        done = object()
        while (item := await asyncio.to_thread(next, replies, done)) is not done:
            yield item

    # --- discovery ------------------------------------------------------------------------

    def list_topics(
        self, type_filter: str | type = "", prefix: str = "", timeout: float = 2.0
    ) -> Iterator[dict]:
        """The objects on the network, as metadata dicts: `topic`, `owner` (its session id),
        `type` (its `$id`), `bases`, and `classes` (the type and its bases).

        `type_filter` is a syncable class, a type `$id`, or a class name. `prefix` limits the
        search to topics under it, and may be relative (`./`).
        """
        from SpiriSynq.schema import type_id

        type_arg = None
        name = ""
        if isinstance(type_filter, type):
            type_arg = type_id(type_filter)
        elif ":" in type_filter:
            type_arg = type_filter
        else:
            name = type_filter.lstrip("!")
        request = self.node.discover(self.resolve_topic(prefix) if prefix else None, type_arg)
        with contextlib.suppress(TimeoutError):
            for found in self.notices(request, timeout):
                classes = [found.type_id, *found.bases]
                if name and not any(_names(c, name) for c in classes):
                    continue
                yield {
                    "topic": found.topic,
                    "owner": found.owner,
                    "type": found.type_id,
                    "bases": list(found.bases),
                    "classes": classes,
                }

    def fetch_schema(self, topic: str, timeout: float | None = None) -> dict:
        """The schema the object on `topic` serves. `LookupError` if nobody answers within
        `timeout` seconds (the call timeout by default).

        Asks again until then: an owner that just appeared may not be reachable yet."""
        key = self.resolve_topic(topic)
        deadline = time.monotonic() + (timeout or self.call_timeout)
        while (left := deadline - time.monotonic()) > 0:
            request = self.node.fetch_schema(key)
            with contextlib.suppress(TimeoutError):
                for notice in self.notices(request, left):
                    if notice.error is not None:
                        raise LookupError(f"{topic}: {notice.error[0]}: {notice.error[1]}")
                    return notice.value
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise LookupError(f"nothing serves {topic}")

    def from_topic_untyped(self, topic: str, **options) -> SyncableObject:
        """Mirror the object on `topic` without its class: one made from the schema it serves,
        with every field typed `Any`."""
        from SpiriSynq.syncable_objects import untyped_class

        schema = self.fetch_schema(topic)
        return untyped_class(schema).from_topic(topic, session=self, **options)

    # --- the threads ----------------------------------------------------------------------

    def _run_pump(self) -> None:
        node = self.node
        while not self._closed.is_set():
            notice = node.next_notice(0.2)  # None once the node is closed
            if notice is None:
                continue
            if isinstance(notice, _REQUEST_NOTICES):
                self._request_queue(notice.request).put(notice)
            else:
                self._callbacks.put(notice)
        self._callbacks.put(None)

    def _run_callbacks(self) -> None:
        while True:
            notice = self._callbacks.get()
            if notice is None:
                return
            self._dispatch(notice)

    def on_network_thread(self) -> bool:
        return threading.current_thread() is self._callback_thread

    def wait(self, done: threading.Event, timeout: float | None) -> bool:
        """Wait for `done`. On the network thread, carry on handling notices meanwhile."""
        if not self.on_network_thread():
            return done.wait(timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        while not done.is_set():
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return False
            try:
                notice = self._callbacks.get(timeout=min(0.1, left or 0.1))
            except queue.Empty:
                continue
            if notice is None:
                self._callbacks.put(None)
                return done.is_set()
            self._dispatch(notice)
        return True

    def _dispatch(self, notice) -> None:
        obj = self._object(notice.object)
        if obj is None:
            return
        try:
            if isinstance(notice, _core.Call):
                self._workers.submit(obj._synq_serve, notice)
            else:
                obj._synq_notice(notice)
        except Exception:
            logger.exception(f"{obj.synq_absolute_path}: error handling {type(notice).__name__}")

    def close(self) -> None:
        """Close every object and the zenoh session."""
        if self._closed.is_set():
            return
        self._closed.set()
        self.node.close()
        self._workers.shutdown(wait=False, cancel_futures=True)
        _live.discard(self)

    def __del__(self):
        with contextlib.suppress(Exception):
            self.close()


def _names(type_id: str, name: str) -> bool:
    """Whether a type `$id` is of a class called `name`."""
    path = type_id.split(":")[-2] if type_id.startswith("urn:uuid:") else type_id.split(":")[-1]
    return path == name or path.endswith(f".{name}")


_live: weakref.WeakSet[Session] = weakref.WeakSet()


@atexit.register
def _close_all() -> None:
    for session in list(_live):
        with contextlib.suppress(Exception):
            session.close()


class _CurrentSession:
    """The session objects use when they aren't given one: like a `ContextVar`, but the
    default session opens on first use, not on import."""

    def __init__(self):
        import contextvars

        self._var: contextvars.ContextVar[Session | None] = contextvars.ContextVar(
            "current_session", default=None
        )
        self._default: Session | None = None
        self._lock = threading.Lock()

    def get(self) -> Session:
        session = self._var.get()
        if session is not None:
            return session
        with self._lock:
            if self._default is None or self._default._closed.is_set():
                self._default = Session()
            return self._default

    def set(self, session: Session):
        return self._var.set(session)

    def reset(self, token) -> None:
        self._var.reset(token)


current_session = _CurrentSession()
