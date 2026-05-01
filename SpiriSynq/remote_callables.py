import asyncio
import zenoh
from typing import TYPE_CHECKING, Dict, Any, overload, Callable, TypeVar
import inspect
from loguru import logger
import functools
import datetime
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


def zenoh_callback(instance_ref,  parent_ref: ref['SyncableObject'], query:zenoh.Query):
    instance = instance_ref()
    parent = parent_ref()
    logger.debug(f"RPC called {instance._wrapped}")
    params = dict(query.parameters)
    result = instance._wrapped(parent, **params)
    result_enc = parent.session.type_registry.dumps(result)
    query.reply(query.key_expr,payload=result_enc,encoding=zenoh.Encoding.APPLICATION_YAML)


class RemoteMethod:
    def __init__(self, wrapped: Callable[..., T]):
        self._wrapped = wrapped
        functools.update_wrapper(self, wrapped)
        self.queryables = {}
        # Cache the signature for performance
        self._signature = inspect.signature(wrapped)

    def __get__(self, instance: Any, owner: type):
        if instance is None:
            return self
        return _BoundRemoteMethod(self, instance)

    def __call__(self, instance: "SyncableObject", *args: Any, **kwargs: Any) -> Any:
        if instance.authoritive:
            return self._wrapped(instance, *args, **kwargs)       
        # Bind all args to parameter names, converting positional to kwargs
        # Now all_kwargs contains all named parameters         
        bound = self._signature.bind(instance, *args, **kwargs)
        bound.apply_defaults()
        all_kwargs = dict(bound.arguments)

        all_kwargs.pop('self', None)
        all_kwargs = {k:instance.session.type_registry.dumps(v).removesuffix("\n...") for k,v in all_kwargs.items()}
        params = zenoh.Parameters(all_kwargs)
        # ... rest of your RPC logic using all_kwargs for serialization
        path = f"{instance.absolute_path}/{self._wrapped.__name__}"
        selector = zenoh.Selector(path, parameters=params)
        logger.debug(f"Calling remote RPC at {selector}")
        reply = instance.session.zenoh_session.get(selector).recv()
        assert reply.ok
        return instance.session.type_registry.load(reply.ok.payload.to_string())

    def setup_zenoh_callback(self, parent: 'SyncableObject', path: str|None=None, name: str|None=None):
        path = path or parent.absolute_path
        name = name or self._wrapped.__name__
        logger.debug(f"Exposing RPC on {path}/{name}")
        parent_ref = weakref.ref(parent)
        instance_ref = weakref.ref(self)
        queryable = parent.session.zenoh_session.declare_queryable(
            f"{path}/{name}",
            lambda query: zenoh_callback(instance_ref, parent_ref, query)
        )
        self.queryables[f"{path}/{name}"] = queryable

    def __del__(self):
        for key, value in self.queryables.items():  # Fixed: .items()
            value.undeclare()



def remote_method(func: Callable[..., T]) -> Callable[..., T]:

    return RemoteMethod(func)
