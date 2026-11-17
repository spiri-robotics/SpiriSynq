"""Remote methods: `@remote_method` and `@remote_method_raw`, and their caller hooks.

A remote method runs on the object's owner. Called on a mirror, it sends the call over the
network and gives back the owner's result; called on the owner, it runs directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import re
import threading
import types
import typing
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, overload

from SpiriSynq import _core
from SpiriSynq.values import BadValue, from_value, hints_of, to_value

if TYPE_CHECKING:
    from SpiriSynq.syncable_objects import SyncableObject

YAML = "application/yaml"
BYTES = "zenoh/bytes"

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::\*\*)?\}")


class RpcException(Exception):  # noqa: N818 - its name since v1
    """An exception raised by a remote method on its owner, raised again on the caller."""

    def __init__(self, kind: str, message: str = "", topic: str | None = None):
        self.kind = kind
        self.message = message
        self.topic = topic
        prefix = f"[{topic}] " if topic else ""
        super().__init__(f"{prefix}{kind}: {message}" if message else f"{prefix}{kind}")


@dataclass
class Query:
    """An incoming call, as it arrived: what a parameter annotated `Query` gets."""

    key: str
    """The key it was called on."""
    parameters: dict[str, str] = field(default_factory=dict)
    """The selector's parameters, such as an auth token."""
    payload: bytes | None = None
    """The query's payload: a raw remote method's own data. `None` for other methods."""
    encoding: str | None = None
    attachment: bytes | None = None


def parse_parameters(text: str) -> dict[str, str]:
    """`a=1;b=2` as `{"a": "1", "b": "2"}`."""
    out = {}
    for part in text.split(";"):
        if part:
            name, _, value = part.partition("=")
            out[name] = value
    return out


def format_parameters(parameters: str | dict[str, str] | None) -> str:
    if parameters is None:
        return ""
    if isinstance(parameters, str):
        return parameters
    return ";".join(f"{k}={v}" for k, v in parameters.items())


class Reply:
    """One reply to a call: its `payload` (bytes), `encoding` and `attachment`.

    A raw remote method yields them; an `after_call` reads them. `Reply.encode(value)` and
    `reply.decode()` write and read a value the usual way.
    """

    def __init__(
        self,
        payload: bytes = b"",
        encoding: str = BYTES,
        attachment: bytes | None = None,
        *,
        _hint: Any = Any,
    ):
        self.payload = bytes(payload)
        self.encoding = encoding
        self.attachment = attachment
        self._hint = _hint

    @classmethod
    def encode(cls, value: Any) -> Reply:
        """A reply carrying `value`, encoded the usual way."""
        return cls(_core.yaml_dump(to_value(value)), YAML)

    def decode(self) -> Any:
        """The value this reply carries, decoded the usual way."""
        if self.encoding != YAML:
            raise ValueError(f"a {self.encoding} reply isn't encoded the usual way")
        return from_value(_core.yaml_load(self.payload), self._hint)

    @classmethod
    def _from_sample(cls, sample: _core.Sample, hint: Any = Any) -> Reply:
        return cls(sample.payload, sample.encoding, sample.attachment, _hint=hint)

    def _sample(self) -> _core.Sample:
        return _core.Sample(self.payload, self.encoding, self.attachment)

    def __repr__(self) -> str:
        return f"Reply({self.payload[:32]!r}, encoding={self.encoding!r})"


class Call:
    """What a `before_call` sends the call with, exactly once."""

    def __init__(self, bound: BoundRemoteMethod, key_args: dict[str, str], raw_replies: bool):
        self._bound = bound
        self._key_args = key_args
        self._raw_replies = raw_replies
        self.request = None

    def _once(self):
        if self.request is not None:
            raise RuntimeError("a before_call sends its call once")

    async def send_args(self, args: dict[str, Any]) -> None:
        """Send the call with these arguments, by name, encoded the usual way."""
        self._once()
        self.request = self._bound._send(args, raw_replies=self._raw_replies)

    async def send(
        self,
        payload: bytes = b"",
        *,
        parameters: str | dict[str, str] | None = None,
        encoding: str = BYTES,
        attachment: bytes | None = None,
    ) -> None:
        """Send the call as these bytes, with these selector parameters."""
        self._once()
        sample = _core.Sample(bytes(payload), encoding, attachment)
        self.request = self._bound._send_raw(
            self._key_args, sample, format_parameters(parameters), self._raw_replies
        )


def _is_query(hint) -> bool:
    if hint is Query:
        return True
    origin = typing.get_origin(hint)
    if origin is typing.Union or origin is types.UnionType:
        return Query in typing.get_args(hint)
    return False


class RemoteMethod:
    """A method that runs on the object's owner. See the module docs."""

    def __init__(
        self,
        func: Callable,
        *,
        key: str | None = None,
        timeout: float | None = None,
        raw: bool = False,
        generator: bool | None = None,
    ):
        functools.update_wrapper(self, func)  # first: it copies the function's __dict__
        self.func = func
        self.name = func.__name__
        self.key = key
        self.timeout = timeout
        self.raw = raw
        self.before_call_func: Callable | None = None
        self.after_call_func: Callable | None = None
        if raw:
            if not inspect.isasyncgenfunction(func):
                raise TypeError(f"{func.__qualname__}: a raw remote method is an async generator")
            self.is_generator = bool(generator)
        else:
            self.is_generator = inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(
                func
            )

    # The hooks: `@m.before_call` and `@m.after_call` keep the attribute bound to `m`.
    def before_call(self, func: Callable) -> RemoteMethod:
        """Run `func(self, call, *args)` on the caller to send the call."""
        self.before_call_func = func
        return self

    def after_call(self, func: Callable) -> RemoteMethod:
        """Run `func(self, replies)` on the caller: it yields what the caller gets."""
        self.after_call_func = func
        return self

    def __set_name__(self, owner, name):
        self.name = name

    @overload
    def __get__(self, obj: None, objtype: type) -> RemoteMethod: ...
    @overload
    def __get__(self, obj: object, objtype: type) -> BoundRemoteMethod: ...
    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return BoundRemoteMethod(self, obj)

    def __call__(self, obj, *args, **kwargs):
        return BoundRemoteMethod(self, obj)(*args, **kwargs)

    # --- what callers see ---------------------------------------------------------------------

    def caller_signature(self) -> tuple[Callable, int]:
        """The function whose parameters callers pass, and how many leading ones they don't."""
        if self.before_call_func is not None:
            return self.before_call_func, 2  # self, call
        return self.func, 2 if self.raw else 1  # self (and the raw method's query)

    def query_params(self, func: Callable) -> set[str]:
        """The parameters of `func` that get the incoming `Query`."""
        if func is self.before_call_func:
            return set()
        hints = hints_of(func)
        return {name for name, hint in hints.items() if name != "return" and _is_query(hint)}

    def placeholders(self) -> list[str]:
        return _PLACEHOLDER.findall(self.key or "")

    def return_hint(self) -> Any:
        """What each value a caller gets is: the method's return type, or what it yields."""
        if self.after_call_func is not None:
            hint = hints_of(self.after_call_func).get("return", Any)
            return _yielded(hint)
        if self.raw:
            return Any  # YAML replies decoded, others as their payload
        hint = hints_of(self.func).get("return", Any)
        return _yielded(hint) if self.is_generator else hint

    # --- on the owner -------------------------------------------------------------------------

    def serve(self, obj: SyncableObject, call: _core.Call) -> None:
        """Answer one incoming call. Runs on a worker thread."""
        node = obj.synq_session.node
        try:
            kwargs = self._owner_args(obj, call)
            if self.raw:
                query = _query(call)
                replies = self.func(obj, query, **kwargs)
                for reply in _iterate(replies):
                    if not isinstance(reply, Reply):
                        raise TypeError(f"a raw remote method yields Replies, not {reply!r}")
                    node.reply_raw(call.query, reply._sample())
            elif self.is_generator:
                for value in _iterate(self.func(obj, **kwargs)):
                    node.reply(call.query, to_value(value))
            else:
                result = self.func(obj, **kwargs)
                if inspect.isawaitable(result):
                    result = asyncio.run(_awaited(result))
                node.reply(call.query, to_value(result))
            node.finish(call.query)
        except Exception as e:
            # The object may have closed meanwhile, or the node.
            with contextlib.suppress(_core.SynqError):
                node.reply_error(call.query, type(e).__name__, str(e))

    def _owner_args(self, obj, call: _core.Call) -> dict[str, Any]:
        sig = inspect.signature(self.func)
        hints = hints_of(self.func)
        params = list(sig.parameters.values())[2 if self.raw else 1 :]
        given = dict(call.args) if isinstance(call.args, dict) else {}
        if self.raw and call.payload is not None and call.payload.encoding == YAML:
            decoded = _core.yaml_load(call.payload.payload)
            if isinstance(decoded, dict):
                given = decoded
        given.update(call.key_args)
        query = _query(call)
        kwargs = {}
        for p in params:
            hint = hints.get(p.name, Any)
            if _is_query(hint):
                kwargs[p.name] = query
            elif p.name in given:
                try:
                    kwargs[p.name] = from_value(given[p.name], hint)
                except BadValue as e:
                    raise TypeError(f"argument `{p.name}`: {e}") from None
        return kwargs

    def run_local(self, obj, args, kwargs):
        """The owner calling its own remote method: run it directly."""
        if self.raw:
            raise TypeError(f"{self.name} is a raw remote method: only a query can call it")
        for name in self.query_params(self.func):
            kwargs.setdefault(name, None)
        result = self.func(obj, *args, **kwargs)
        if inspect.iscoroutine(result):
            return run_sync(result)
        return result


def _yielded(hint) -> Any:
    args = typing.get_args(hint)
    return args[0] if args else Any


def _query(call: _core.Call) -> Query:
    payload = call.payload
    return Query(
        key=call.key,
        parameters=parse_parameters(call.parameters),
        payload=None if payload is None else payload.payload,
        encoding=None if payload is None else payload.encoding,
        attachment=None if payload is None else payload.attachment,
    )


async def _awaited(awaitable):
    return await awaitable


def _iterate(values) -> Iterator:
    """A generator's values, or an async generator's, in a plain loop."""
    if inspect.isasyncgen(values):
        # A raw or async-generator method's replies, in order; each sent as it comes.
        loop = asyncio.new_event_loop()
        try:
            while True:
                try:
                    yield loop.run_until_complete(values.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            loop.run_until_complete(values.aclose())
            loop.close()
    else:
        yield from values


# --- the hook loop ----------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def hook_loop() -> asyncio.AbstractEventLoop:
    """An event loop on a thread of its own, for the async hooks of blocking calls."""
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="SpiriSynq hooks", daemon=True).start()
        return _loop


def run_sync(coroutine):
    """Run a coroutine to the end, from code that isn't async."""
    return asyncio.run_coroutine_threadsafe(coroutine, hook_loop()).result()


def iterate_sync(agen) -> Iterator:
    """An async generator's values, from code that isn't async."""
    loop = hook_loop()
    try:
        while True:
            try:
                yield asyncio.run_coroutine_threadsafe(agen.__anext__(), loop).result()
            except StopAsyncIteration:
                return
    finally:
        asyncio.run_coroutine_threadsafe(agen.aclose(), loop).result()


# --- on the caller ----------------------------------------------------------------------------


class BoundRemoteMethod:
    """A remote method of one object. Call it, or `as_async(...)` it."""

    def __init__(self, method: RemoteMethod, obj: SyncableObject):
        self._method = method
        self._obj = obj
        functools.update_wrapper(self, method.func)

    def __call__(self, *args, **kwargs):
        m, obj = self._method, self._obj
        if obj.synq_authoritive:
            return m.run_local(obj, args, kwargs)
        results = self._results(args, kwargs)
        if m.is_generator:
            return results
        return _one(results)

    def as_async(self, *args, **kwargs):
        """An awaitable for the result, or an async iterator for a generator method."""
        m, obj = self._method, self._obj
        if obj.synq_authoritive:
            if m.raw:
                raise TypeError(f"{m.name} is a raw remote method: only a query can call it")
            for name in m.query_params(m.func):
                kwargs.setdefault(name, None)
            if m.is_generator:
                return _local_agen(m.func(obj, *args, **kwargs))
            return _local_await(m.func, obj, args, kwargs)
        results = self._aresults(args, kwargs)
        if m.is_generator:
            return results
        return _aone(results)

    def pipe(self, *args, **kwargs):
        """Call chaining isn't in this release."""
        raise NotImplementedError("call chaining with .pipe() isn't in this release")

    # --- sending ------------------------------------------------------------------------------

    def _timeout_ms(self) -> int | None:
        timeout = self._method.timeout or self._obj.synq_session.call_timeout
        return None if timeout is None else int(timeout * 1000)

    def _send(self, args: dict[str, Any], *, raw_replies: bool):
        node, obj = self._obj.synq_session.node, self._obj
        values = {k: to_value(v) for k, v in args.items()}
        return node.call(
            obj._synq_id,
            self._method.name,
            values,
            raw_replies=raw_replies,
            timeout_ms=self._timeout_ms(),
        )

    def _send_raw(self, key_args, sample, parameters, raw_replies):
        node, obj = self._obj.synq_session.node, self._obj
        return node.call_raw(
            obj._synq_id,
            self._method.name,
            key_args,
            sample,
            raw_replies=raw_replies,
            parameters=parameters,
            timeout_ms=self._timeout_ms(),
        )

    def _bind(self, args, kwargs) -> dict[str, Any]:
        func, skip = self._method.caller_signature()
        sig = inspect.signature(func)
        params = list(sig.parameters.values())[skip:]
        leave_out = self._method.query_params(func)
        sig = sig.replace(parameters=[p for p in params if p.name not in leave_out])
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)

    def _python_hook(self, slot: str):
        from SpiriSynq.wasm import WasmFunction

        hook = getattr(self._method, slot)
        return None if hook is None or isinstance(hook, WasmFunction) else hook

    async def _start(self, args, kwargs):
        """Send the call, through a Python `before_call` if there is one. Its request."""
        m = self._method
        bound = self._bind(args, kwargs)
        raw_replies = True
        before = self._python_hook("before_call_func")
        if before is None:
            return self._send(bound, raw_replies=raw_replies)
        key_args = {k: bound[k] for k in m.placeholders() if k in bound}
        call = Call(self, key_args, raw_replies)
        await before(self._obj, call, **bound)
        if call.request is None:
            raise RuntimeError(f"{m.name}'s before_call returned without sending the call")
        return call.request

    def _decode(self, value, error, hint):
        if error is not None:
            raise RpcException(error[0], error[1], topic=self._obj.synq_absolute_path)
        if isinstance(value, _core.Sample):
            if value.encoding != YAML:
                return value.payload
            value = _core.yaml_load(value.payload)
        try:
            return from_value(value, hint)
        except BadValue as e:
            raise RpcException("BadValue", str(e), topic=self._obj.synq_absolute_path) from None

    def _results(self, args, kwargs) -> Iterator:
        """The values the caller gets, from code that isn't async."""
        if self._python_hook("before_call_func") or self._python_hook("after_call_func"):
            return iterate_sync(self._aresults(args, kwargs))
        request = self._send(self._bind(args, kwargs), raw_replies=True)
        return self._plain(request)

    def _plain(self, request) -> Iterator:
        hint = self._method.return_hint()
        session = self._obj.synq_session
        for value, error in session.replies(request, self._wait()):
            yield self._decode(value, error, hint)

    def _wait(self) -> float | None:
        timeout = self._method.timeout or self._obj.synq_session.call_timeout
        return None if timeout is None else timeout + 5.0

    async def _aresults(self, args, kwargs) -> AsyncIterator:
        """The values the caller gets, from async code."""
        request = await self._start(args, kwargs)
        session = self._obj.synq_session
        after = self._python_hook("after_call_func")
        hint = self._method.return_hint()
        if after is None:
            async for value, error in session.areplies(request, self._wait()):
                yield self._decode(value, error, hint)
            return
        reply_hint = _reply_hint(self._method)

        async def replies():
            async for value, error in session.areplies(request, self._wait()):
                if error is not None:
                    raise RpcException(error[0], error[1], topic=self._obj.synq_absolute_path)
                # A Python after_call's replies are always raw: the core decodes none.
                yield Reply._from_sample(value, reply_hint)

        async for result in after(self._obj, replies()):
            yield result


def _reply_hint(method: RemoteMethod) -> Any:
    """What `reply.decode()` gives in an `after_call`: the method's own return type."""
    if method.raw:
        return Any
    hint = hints_of(method.func).get("return", Any)
    return _yielded(hint) if method.is_generator else hint


def _one(results: Iterator):
    values = list(results)
    if len(values) != 1:
        raise RuntimeError(f"expected one result, got {len(values)}")
    return values[0]


async def _aone(results: AsyncIterator):
    values = [v async for v in results]
    if len(values) != 1:
        raise RuntimeError(f"expected one result, got {len(values)}")
    return values[0]


async def _local_agen(values):
    if inspect.isasyncgen(values):
        async for v in values:
            yield v
    else:
        for v in values:
            yield v


async def _local_await(func, obj, args, kwargs):
    if inspect.iscoroutinefunction(func):
        return await func(obj, *args, **kwargs)
    return await asyncio.to_thread(func, obj, *args, **kwargs)


# --- the decorators ---------------------------------------------------------------------------


def remote_method(
    func: Callable | None = None, *, key: str | None = None, timeout: float | None = None
):
    """Make a method run on the object's owner. `@remote_method` or `@remote_method(key=...,
    timeout=...)`."""
    if func is not None:
        return RemoteMethod(func)
    return lambda f: RemoteMethod(f, key=key, timeout=timeout)


def remote_method_raw(
    func: Callable | None = None,
    *,
    key: str | None = None,
    timeout: float | None = None,
    generator: bool = False,
):
    """A remote method that gets the raw `Query` and yields its `Reply`s itself. With
    `generator=True`, callers get a stream of values, not one."""
    if func is not None:
        return RemoteMethod(func, raw=True)
    return lambda f: RemoteMethod(f, key=key, timeout=timeout, raw=True, generator=generator)
