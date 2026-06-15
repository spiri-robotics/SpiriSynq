import asyncio
import concurrent.futures
import zenoh
from typing import TYPE_CHECKING, Dict, Any, Callable, TypeVar, overload
import inspect
from loguru import logger
import functools
import weakref
from weakref import ref


if TYPE_CHECKING:
    from SpiriSynq.session import Session
    from SpiriSynq.syncable_objects import SyncableObject

class RpcException(Exception):
    pass

# Final reply encoding for generator methods: signals exhaustion and carries the return value.
GENERATOR_DONE_ENCODING = zenoh.Encoding("x-spirisynq/generator-done")

# Sentinel used to detect StopIteration from run_in_executor (which swallows it).
_EXHAUSTED = object()


def rpc_call(topic: str, session: 'Session', kwargs: Dict[str, Any] | None = None):
    if not kwargs:
        kwargs = {}
    yaml = session.type_registry
    z_session = session.zenoh_session
    with session.as_default():
        params = zenoh.Parameters(kwargs)
        selector = zenoh.Selector(topic, params)
        logger.trace(f"Calling {selector}")
        try:
            reply = z_session.get(selector).recv()
            if not reply.ok:
                assert reply.err, "RPC reply not OK and remote didn't return an error message"
                raise RpcException(reply.err.payload.to_string())
            if reply.ok.encoding == zenoh.Encoding.APPLICATION_YAML:
                return yaml.load(reply.ok.payload.to_string())
            raise RpcException(f"Not yaml encoded: {reply.ok.encoding} {reply}")
        except Exception as e:
            logger.error(f"Error making remote call on {selector}: {e}")
            raise e

T = TypeVar('T')


def _run_coroutine_sync(coro):
    """Run a coroutine synchronously, whether or not an event loop is already running."""
    try:
        asyncio.get_running_loop()
        # Running inside an async context — dispatch to a fresh thread with its own loop.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        return asyncio.run(coro)


def _zenoh_callback(instance_ref: 'ref[RemoteMethod]', parent_ref: 'ref[SyncableObject]'):
    def callback(query: zenoh.Query):
        instance = instance_ref()
        parent = parent_ref()
        if instance is None or parent is None:
            logger.warning("RPC callback fired after owner was collected; ignoring.")
            return

        logger.trace(f"RPC called {query.key_expr}?{query.parameters}")
        registry = parent.synq_session.type_registry
        params = {k: registry.load(v) for k, v in dict(query.parameters).items()}
        try:
            if instance._is_generator:
                gen = instance._wrapped(parent, **params)
                try:
                    while True:
                        try:
                            value = next(gen)
                        except StopIteration as e:
                            query.reply(
                                query.key_expr,
                                payload=registry.dumps(e.value),
                                encoding=GENERATOR_DONE_ENCODING,
                            )
                            break
                        query.reply(
                            query.key_expr,
                            payload=registry.dumps(value),
                            encoding=zenoh.Encoding.APPLICATION_YAML,
                        )
                except Exception:
                    raise  # caught by outer handler, which sends reply_err

            elif instance._is_async_gen:
                async def _run():
                    async for value in instance._wrapped(parent, **params):
                        query.reply(
                            query.key_expr,
                            payload=registry.dumps(value),
                            encoding=zenoh.Encoding.APPLICATION_YAML,
                        )
                    # async generators cannot carry a return value
                    query.reply(
                        query.key_expr,
                        payload=registry.dumps(None),
                        encoding=GENERATOR_DONE_ENCODING,
                    )
                asyncio.run(_run())

            elif instance._is_async:
                result = asyncio.run(instance._wrapped(parent, **params))
                query.reply(
                    query.key_expr,
                    payload=registry.dumps(result),
                    encoding=zenoh.Encoding.APPLICATION_YAML,
                )

            else:
                result = instance._wrapped(parent, **params)
                query.reply(
                    query.key_expr,
                    payload=registry.dumps(result),
                    encoding=zenoh.Encoding.APPLICATION_YAML,
                )

        except Exception as e:
            query.reply_err(f"RPC error '{e}'")
            logger.error(f"Local RPC error '{e}'")

    return callback


class BoundRemoteMethod:
    """A RemoteMethod bound to a specific instance, exposing both sync and async interfaces."""

    def __init__(self, remote_method: 'RemoteMethod', instance: Any, *, _timeout: float | None = None):
        self._remote_method = remote_method
        self._instance = instance
        self._timeout = _timeout
        functools.update_wrapper(self, remote_method._wrapped)

    def timeout(self, seconds: float) -> 'BoundRemoteMethod':
        return BoundRemoteMethod(self._remote_method, self._instance, _timeout=seconds)

    # --- client-side transforms ---

    def _apply_client(self, result):
        func = self._remote_method._client_func
        if func is None:
            return result
        return func(self._instance, result)

    def _wrap_gen_with_client(self, gen):
        func = self._remote_method._client_func
        if func is None:
            return (yield from gen)
        for item in gen:
            yield func(self._instance, item)

    # --- remote dispatch (all read self._timeout directly) ---

    def _build_selector(self, *args, **kwargs):
        rm = self._remote_method
        instance = self._instance
        bound = rm._signature.bind(instance, *args, **kwargs)
        bound.apply_defaults()
        all_kwargs = {
            k: instance.synq_session.type_registry.dumps(v).removesuffix("\n...")
            for k, v in bound.arguments.items()
            if k != 'self'
        }
        return zenoh.Selector(
            f"{instance.synq_absolute_path}/{rm._wrapped.__name__}",
            parameters=zenoh.Parameters(all_kwargs),
        )

    def _execute_remote(self, *args, **kwargs):
        selector = self._build_selector(*args, **kwargs)
        reply = self._instance.synq_session.zenoh_session.get(
            selector, timeout=self._timeout
        ).recv()
        if reply.err:
            raise RpcException(reply.err.payload.to_string())
        return self._instance.synq_session.type_registry.load(reply.ok.payload.to_string())

    def _remote_generator(self, *args, **kwargs):
        selector = self._build_selector(*args, **kwargs)
        # ConsolidationMode.NONE is required: default (AUTO) keeps only the last
        # reply per key, which would drop all yielded values except the final one.
        for reply in self._instance.synq_session.zenoh_session.get(
            selector,
            consolidation=zenoh.QueryConsolidation(zenoh.ConsolidationMode.NONE),
            timeout=self._timeout,
        ):
            if reply.err:
                raise RpcException(reply.err.payload.to_string())
            if reply.ok.encoding == GENERATOR_DONE_ENCODING:
                return self._instance.synq_session.type_registry.load(reply.ok.payload.to_string())
            yield self._instance.synq_session.type_registry.load(reply.ok.payload.to_string())

    async def _call_async(self, *args, **kwargs):
        rm = self._remote_method
        instance = self._instance
        if instance.synq_authoritive:
            return await rm._execute_local_async(instance, *args, **kwargs)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, functools.partial(self._execute_remote, *args, **kwargs))
        return self._apply_client(result)

    async def _async_gen(self, *args, **kwargs):
        rm = self._remote_method
        instance = self._instance
        if instance.synq_authoritive:
            if rm._is_async_gen:
                async for value in rm._wrapped(instance, *args, **kwargs):
                    yield value
                return
            gen = rm._wrapped(instance, *args, **kwargs)
            while True:
                value = next(gen, _EXHAUSTED)
                if value is _EXHAUSTED:
                    return
                yield value
        else:
            assert instance.synq_session
            loop = asyncio.get_event_loop()
            gen = self._remote_generator(*args, **kwargs)
            while True:
                value = await loop.run_in_executor(None, next, gen, _EXHAUSTED)
                if value is _EXHAUSTED:
                    return
                yield self._apply_client(value)

    # --- public interface ---

    def __call__(self, *args, **kwargs):
        rm = self._remote_method
        instance = self._instance
        if instance.synq_authoritive:
            if rm._is_generator or rm._is_async_gen:
                return rm._wrapped(instance, *args, **kwargs)
            return rm._execute_local(instance, *args, **kwargs)
        assert instance.synq_session
        if rm._is_generator or rm._is_async_gen:
            return self._wrap_gen_with_client(self._remote_generator(*args, **kwargs))
        return self._apply_client(self._execute_remote(*args, **kwargs))

    def sync(self, *args, **kwargs):
        return self(*args, **kwargs)

    def as_async(self, *args, **kwargs):
        """Return a coroutine (regular methods) or async generator (generator methods)."""
        rm = self._remote_method
        if rm._is_generator or rm._is_async_gen:
            return self._async_gen(*args, **kwargs)
        return self._call_async(*args, **kwargs)

    def setup_zenoh_callback(self, parent: 'SyncableObject', path: str | None = None, name: str | None = None):
        self._remote_method.setup_zenoh_callback(parent, path=path, name=name)


class RemoteMethod:
    def __init__(self, wrapped: Callable[..., T]):
        self._wrapped = wrapped
        self._is_generator = inspect.isgeneratorfunction(wrapped)
        self._is_async_gen = inspect.isasyncgenfunction(wrapped)
        self._is_async = asyncio.iscoroutinefunction(wrapped)  # excludes async generators
        self._client_func: Callable | None = None
        functools.update_wrapper(self, wrapped)
        self._signature = inspect.signature(wrapped)

    def client(self, func: Callable) -> 'RemoteMethod':
        self._client_func = func
        return self

    @overload
    def __get__(self, instance: None, owner: type) -> 'RemoteMethod': ...
    @overload
    def __get__(self, instance: object, owner: type) -> 'BoundRemoteMethod': ...
    def __get__(self, instance, owner):  # type: ignore[misc]
        if instance is None:
            return self
        return BoundRemoteMethod(self, weakref.proxy(instance))

    def _execute_local(self, instance, *args, **kwargs):
        if self._is_async:
            return _run_coroutine_sync(self._wrapped(instance, *args, **kwargs))
        return self._wrapped(instance, *args, **kwargs)

    async def _execute_local_async(self, instance, *args, **kwargs):
        if self._is_async:
            return await self._wrapped(instance, *args, **kwargs)  # type: ignore[misc]
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, functools.partial(self._wrapped, instance, *args, **kwargs)
        )

    def __call__(self, instance: 'SyncableObject', *args: Any, **kwargs: Any) -> Any:
        """Unbound call — delegates to a transient BoundRemoteMethod with no timeout."""
        return BoundRemoteMethod(self, instance)(*args, **kwargs)

    def setup_zenoh_callback(self, parent: 'SyncableObject', path: str | None = None, name: str | None = None):
        key = f"{path or parent.synq_absolute_path}/{name or self._wrapped.__name__}"
        logger.debug(f"Exposing RPC {parent.__class__.__name__}.{self._wrapped.__name__} on {key}")

        queryable = parent.synq_session.zenoh_session.declare_queryable(
            key,
            _zenoh_callback(weakref.ref(self), weakref.ref(parent)),
        )

        parent._synq_callbacks[key] = queryable


def undeclare(key, queryable):
    def _undeclare():
        logger.debug(f"Undeclaring {key}")
        queryable.undeclare()
    return _undeclare

def remote_method(func: Callable[..., T]) -> RemoteMethod:
    return RemoteMethod(func)
