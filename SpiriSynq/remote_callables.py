import asyncio
import zenoh
from typing import TYPE_CHECKING, Dict, Any, overload, Callable, TypeVar
import inspect
from loguru import logger
import functools
import datetime
import weakref

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
        raise


T = TypeVar('T')

class _BoundRemoteMethod:
    """Wraps a RemoteMethod descriptor bound to an instance."""
    # __slots__ = ('_descriptor', '_instance', )

    def __init__(self, descriptor: 'RemoteMethod', instance: Any):
        self._descriptor = descriptor
        self._instance = instance
        functools.update_wrapper(self, descriptor._wrapped)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Prepend the bound instance so the underlying function receives 'self'
        return self._descriptor(self._instance, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Proxy attribute access back to the descriptor (e.g. setup_zenoh_callbacks)
        return getattr(self._descriptor, name)


class RemoteMethod:
    def __init__(self, wrapped: Callable[..., T]):
        self._wrapped = wrapped
        functools.update_wrapper(self, wrapped)
        

    def __get__(self, instance: Any, owner: type):
        if instance is None:
            return self
        return _BoundRemoteMethod(self, instance)

    def __call__(self, instance: "SyncableObject", *args: Any, **kwargs: Any) -> Any:
            if instance.authoritive:
                return self._wrapped(instance, *args, **kwargs)
            # instance.session.zenoh_session
    
    def zenoh_callback(self):
        logger.debug(f"RPC called {self}")

    def setup_zenoh_callback(self, parent: 'SyncableObject', path: str|None=None, name:str|None=None):
        path = path or parent.absolute_path
        name = name or self._wrapped.__name__
        logger.debug(f"Exposing RPC on {path}/{name}")


def remote_method(func: Callable[..., T]) -> Callable[..., T]:

    return RemoteMethod(func)
