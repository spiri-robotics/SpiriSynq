import asyncio
import zenoh
from typing import TYPE_CHECKING, Dict, Any, Callable, TypeVar, cast, Iterator, types
import inspect
from loguru import logger
import functools
import weakref
from weakref import ref

if TYPE_CHECKING:
    from SpiriSynq.session import SyncableObject, Session
class RpcException(Exception):
    pass

@logger.catch
def rpc_call(topic: str, session: 'Session', kwargs: Dict[str, Any] | None = None):
    if not kwargs:
        kwargs = {}
    yaml = session.type_registry
    z_session = session.zenoh_session
    params = zenoh.Parameters(kwargs)
    selector = zenoh.Selector(topic, params)
    logger.debug(f"Calling {selector}")
    try:
        reply = z_session.get(selector).recv()
        if not reply.ok:
            assert reply.err, "RPC reply not OK and remote didn't return an error message"
            raise RpcException(reply.err.payload.to_string())
        if reply.ok.encoding == zenoh.Encoding.APPLICATION_YAML:
            return yaml.load(reply.ok.payload.to_string())
        raise RpcException(f"Not yaml: {reply}")
    except Exception as e:
        logger.error(f"Error making remote call on {selector}: {e}")
        raise e

T = TypeVar('T')

def _zenoh_callback(instance_ref: 'ref[RemoteMethod]', parent_ref: 'ref[SyncableObject]'):
    def callback(query: zenoh.Query):
        instance = instance_ref()
        parent = parent_ref()
        if instance is None or parent is None:
            logger.warning("RPC callback fired after owner was collected; ignoring.")
            return

        logger.debug(f"RPC called {instance._wrapped}")
        params = dict(query.parameters)
        result = instance._wrapped(parent, **params)
        result_enc = parent.session.type_registry.dumps(result)

        query.reply(
            query.key_expr,
            payload=result_enc,
            encoding=zenoh.Encoding.APPLICATION_YAML,
        )
    return weakref.proxy(callback)


class RemoteMethod:
    def __init__(self, wrapped: Callable[..., T]):
        self._wrapped = wrapped
        functools.update_wrapper(self, wrapped)
        self._signature = inspect.signature(wrapped)

    def __get__(self, instance: Any, owner: type):
        if instance is None:
            return self
        return types.MethodType(self, instance)  # Standard bound method

    def __call__(self, instance: 'SyncableObject', *args: Any, **kwargs: Any) -> Any:
        if instance.authoritive:
            return self._wrapped(instance, *args, **kwargs)

        assert instance.session

        bound = self._signature.bind(instance, *args, **kwargs)
        bound.apply_defaults()

        all_kwargs = {
            k: instance.session.type_registry.dumps(v).removesuffix("\n...")
            for k, v in bound.arguments.items()
            if k != 'self'
        }

        selector = zenoh.Selector(
            f"{instance.absolute_path}/{self._wrapped.__name__}",
            parameters=zenoh.Parameters(all_kwargs),
        )

        logger.debug(f"Calling remote RPC at {selector}")
        reply = instance.session.zenoh_session.get(selector).recv()
        assert reply.ok

        return instance.session.type_registry.load(reply.ok.payload.to_string())

    def setup_zenoh_callback(self, parent: 'SyncableObject', path: str | None = None, name: str | None = None):
        key = f"{path or parent.absolute_path}/{name or self._wrapped.__name__}"
        logger.debug(f"Exposing RPC on {key}")

        queryable = parent.session.zenoh_session.declare_queryable(
            key,
            _zenoh_callback(weakref.ref(self), weakref.ref(parent)),
        )

        weakref.finalize(parent, queryable.undeclare)
        weakref.finalize(parent, lambda: logger.warning(f"Undeclared {key}"))


def remote_method(func: Callable[..., T]) -> Callable[..., T]:
    return RemoteMethod(func)
