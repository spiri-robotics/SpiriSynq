"""
Integration tests for @remote_method / RPC functionality.
"""

import time
from dataclasses import dataclass

import pytest

from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session
from SpiriSynq.remote_callables import remote_method, RpcException


@pytest.fixture(autouse=True)
def close_test_sessions():
    from SpiriSynq.shutdown import _live_sessions
    before = set(_live_sessions.keys())
    yield
    for sid, session in list(_live_sessions.items()):
        if sid not in before:
            session.close()


def _wait_for(predicate, timeout=1.0, interval=0.01):
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_remote_method_basic_call():
    """
    A @remote_method called on a non-authoritative session should execute
    on the authoritative session and return the result.
    """
    @dataclass
    class WithRpc(SyncableObject):
        @remote_method()
        def hello(self, name: str) -> str:
            return f"hello {name}"

    obj = WithRpc("test/rpc_basic", synq_authoritive=True)
    session_b = Session()
    remote = WithRpc.from_topic(obj.synq_absolute_path, session=session_b)

    result = remote.hello("world")
    assert result == "hello world"


def test_remote_method_authoritative_executes_locally():
    """
    Calling a @remote_method on the authoritative object should run the
    function in-process without a zenoh round-trip.
    """
    call_count = {"n": 0}

    @dataclass
    class TrackingRpc(SyncableObject):
        @remote_method()
        def increment(self) -> int:
            call_count["n"] += 1
            return call_count["n"]

    obj = TrackingRpc("test/rpc_local", synq_authoritive=True)
    result = obj.increment()
    assert result == 1
    assert call_count["n"] == 1


def test_remote_method_side_effects():
    """
    A @remote_method that mutates authoritative state should have those
    changes propagated to remote observers.
    """
    @dataclass
    class WithMutableRpc(SyncableObject):
        value: int = 0

        @remote_method()
        def set_value(self, new_value: int) -> None:
            self.value = new_value

    obj = WithMutableRpc("test/rpc_side_effects", synq_authoritive=True, value=0)
    session_b = Session()
    remote = WithMutableRpc.from_topic(obj.synq_absolute_path, session=session_b)

    remote.set_value(42)
    assert obj.value == 42, "Authoritative value not updated after RPC"
    assert _wait_for(lambda: remote.value == 42), "Timeout: remote value not propagated"


def test_remote_method_exception_propagation():
    """
    When a @remote_method raises on the authoritative side, RpcException
    should be raised on the caller.
    """
    @dataclass
    class WithCrash(SyncableObject):
        @remote_method()
        def crash(self) -> None:
            raise ValueError("intentional crash")

    obj = WithCrash("test/rpc_exception", synq_authoritive=True)
    session_b = Session()
    remote = WithCrash.from_topic(obj.synq_absolute_path, session=session_b)

    with pytest.raises(RpcException):
        remote.crash()


def test_builtin_sr_rehydrate():
    """
    sr_rehydrate should return the full current state of the authoritative object.
    """
    @dataclass
    class RehydrateTest(SyncableObject):
        value: int = 0
        label: str = ""

    obj = RehydrateTest("test/rpc_rehydrate", synq_authoritive=True, value=7, label="hi")
    session_b = Session()
    remote = RehydrateTest.from_topic(obj.synq_absolute_path, session=session_b)

    fresh = remote.sr_rehydrate()
    assert isinstance(fresh, RehydrateTest)
    assert fresh.value == 7
    assert fresh.label == "hi"


def test_builtin_sr_metadata():
    """
    sr_metadata should return the topic path, class tags, and authoritative node id.
    """
    @dataclass
    class MetaTest(SyncableObject):
        value: int = 0

    obj = MetaTest("test/rpc_metadata", synq_authoritive=True)
    session_b = Session()
    remote = MetaTest.from_topic(obj.synq_absolute_path, session=session_b)

    meta = remote.sr_metadata()
    assert meta["topic"] == obj.synq_absolute_path
    assert any("MetaTest" in tag for tag in meta["classes"])
    assert "authoritive_node" in meta


def test_builtin_sr_object_schema():
    """
    sr_object_schema should return the JSON Schema including syncable fields
    and RPC endpoint definitions.
    """
    @dataclass
    class SchemaTest(SyncableObject):
        speed: float = 0.0

        @remote_method()
        def set_speed(self, new_speed: float) -> None:
            self.speed = new_speed

    obj = SchemaTest("test/rpc_schema", synq_authoritive=True)
    session_b = Session()
    remote = SchemaTest.from_topic(obj.synq_absolute_path, session=session_b)

    schema = remote.sr_object_schema()
    assert "properties" in schema
    assert "speed" in schema["properties"]
    assert "x-rpc-endpoints" in schema
    assert "set_speed" in schema["x-rpc-endpoints"]


def test_async_remote_method_sync_call():
    """
    An async @remote_method called synchronously on the authoritative side
    should block and return the result.
    """
    @dataclass
    class WithAsync(SyncableObject):
        @remote_method()
        async def greet(self, name: str) -> str:
            return f"async hello {name}"

    obj = WithAsync("test/rpc_async_sync", synq_authoritive=True)
    result = obj.greet("world")
    assert result == "async hello world"


def test_async_remote_method_over_network():
    """
    An async @remote_method called on a non-authoritative session should
    execute on the authoritative side and return the result.
    """
    @dataclass
    class WithAsyncRpc(SyncableObject):
        @remote_method()
        async def compute(self, x: int) -> int:
            return x * 2

    obj = WithAsyncRpc("test/rpc_async_remote", synq_authoritive=True)
    session_b = Session()
    remote = WithAsyncRpc.from_topic(obj.synq_absolute_path, session=session_b)

    result = remote.compute(21)
    assert result == 42


def test_as_async_on_sync_method():
    """
    .as_async on a sync @remote_method should return a coroutine that produces
    the correct result.
    """
    import asyncio

    @dataclass
    class WithSyncRpc(SyncableObject):
        @remote_method()
        def double(self, x: int) -> int:
            return x * 2

    obj = WithSyncRpc("test/rpc_as_async_sync", synq_authoritive=True)
    result = asyncio.run(obj.double.as_async(7))
    assert result == 14


def test_as_async_on_async_method():
    """
    .as_async on an async @remote_method should return a coroutine that
    awaits the underlying async function.
    """
    import asyncio

    @dataclass
    class WithAsyncMethod(SyncableObject):
        @remote_method()
        async def triple(self, x: int) -> int:
            return x * 3

    obj = WithAsyncMethod("test/rpc_as_async_async", synq_authoritive=True)
    result = asyncio.run(obj.triple.as_async(5))
    assert result == 15


def test_as_async_remote_call():
    """
    .as_async on a non-authoritative instance should run the zenoh RPC call
    in an executor and return the result as a coroutine.
    """
    import asyncio

    @dataclass
    class WithRemoteAsync(SyncableObject):
        @remote_method()
        def add(self, a: int, b: int) -> int:
            return a + b

    obj = WithRemoteAsync("test/rpc_as_async_remote", synq_authoritive=True)
    session_b = Session()
    remote = WithRemoteAsync.from_topic(obj.synq_absolute_path, session=session_b)

    result = asyncio.run(remote.add.as_async(3, 4))
    assert result == 7


def test_generator_remote_method_local():
    """
    A generator @remote_method on the authoritative side should yield values
    and expose the return value via StopIteration.
    """
    @dataclass
    class WithGen(SyncableObject):
        @remote_method()
        def count(self, n: int):
            for i in range(n):
                yield i
            return "done"

    obj = WithGen("test/rpc_gen_local", synq_authoritive=True)
    gen = obj.count(3)
    results = []
    return_value = None
    try:
        while True:
            results.append(next(gen))
    except StopIteration as e:
        return_value = e.value

    assert results == [0, 1, 2]
    assert return_value == "done"


def test_generator_remote_method_over_network():
    """
    A generator @remote_method called on a non-authoritative session should
    stream values over zenoh and expose the return value.
    """
    @dataclass
    class WithGenRpc(SyncableObject):
        @remote_method()
        def squares(self, n: int):
            for i in range(n):
                yield i * i
            return "finished"

    obj = WithGenRpc("test/rpc_gen_remote", synq_authoritive=True)
    session_b = Session()
    remote = WithGenRpc.from_topic(obj.synq_absolute_path, session=session_b)

    gen = remote.squares(4)
    results = []
    return_value = None
    try:
        while True:
            results.append(next(gen))
    except StopIteration as e:
        return_value = e.value

    assert results == [0, 1, 4, 9]
    assert return_value == "finished"


def test_generator_remote_method_as_async():
    """
    .as_async on a generator @remote_method should return an async generator
    that yields the same values.
    """
    @dataclass
    class WithGenAsync(SyncableObject):
        @remote_method()
        def evens(self, n: int):
            for i in range(n):
                yield i * 2

    import asyncio

    async def collect():
        obj = WithGenAsync("test/rpc_gen_as_async", synq_authoritive=True)
        results = []
        async for value in obj.evens.as_async(4):
            results.append(value)
        return results

    assert asyncio.run(collect()) == [0, 2, 4, 6]


def test_client_transform_applied_on_remote_call():
    """
    @method.client should post-process the return value on non-authoritative callers.
    """
    @dataclass
    class WithClient(SyncableObject):
        @remote_method()
        def get_value(self) -> int:
            return 10

        @get_value.client()
        def get_value(self, result: int) -> int:
            return result * 2

    obj = WithClient("test/rpc_client_basic", synq_authoritive=True)
    session_b = Session()
    remote = WithClient.from_topic(obj.synq_absolute_path, session=session_b)

    assert remote.get_value() == 20


def test_client_transform_not_applied_on_authoritative():
    """
    @method.client should NOT run when called on the authoritative object.
    """
    @dataclass
    class WithClient(SyncableObject):
        @remote_method()
        def get_value(self) -> int:
            return 10

        @get_value.client()
        def get_value(self, result: int) -> int:
            return result * 2

    obj = WithClient("test/rpc_client_authoritative", synq_authoritive=True)
    assert obj.get_value() == 10


def test_client_transform_receives_self():
    """
    The client func receives the non-authoritative instance as `self`.
    """
    @dataclass
    class WithClientSelf(SyncableObject):
        multiplier: int = 3

        @remote_method()
        def get_value(self) -> int:
            return 7

        @get_value.client()
        def get_value(self, result: int) -> int:
            return result * self.multiplier

    obj = WithClientSelf("test/rpc_client_self", synq_authoritive=True, multiplier=3)
    session_b = Session()
    remote = WithClientSelf.from_topic(obj.synq_absolute_path, session=session_b)

    assert remote.get_value() == 21


def test_client_transform_on_generator():
    """
    @method.client on a generator method applies the transform per yielded item.
    """
    @dataclass
    class WithClientGen(SyncableObject):
        @remote_method()
        def numbers(self, n: int):
            for i in range(n):
                yield i
            return "done"

        @numbers.client()
        def numbers(self, item: int) -> str:
            return f"item:{item}"

    obj = WithClientGen("test/rpc_client_gen", synq_authoritive=True)
    session_b = Session()
    remote = WithClientGen.from_topic(obj.synq_absolute_path, session=session_b)

    assert list(remote.numbers(3)) == ["item:0", "item:1", "item:2"]


def test_client_transform_on_async_remote():
    """
    @method.client applies the transform when using .as_async on a remote call.
    """
    import asyncio

    @dataclass
    class WithClientAsync(SyncableObject):
        @remote_method()
        def double(self, x: int) -> int:
            return x * 2

        @double.client()
        def double(self, result: int) -> int:
            return result + 1

    obj = WithClientAsync("test/rpc_client_async", synq_authoritive=True)
    session_b = Session()
    remote = WithClientAsync.from_topic(obj.synq_absolute_path, session=session_b)

    result = asyncio.run(remote.double.as_async(5))
    assert result == 11  # (5*2) + 1


def test_server_hook_intercepts_query():
    """
    @method.server() should be called instead of the default dispatch when
    a zenoh query arrives, and can send a custom reply.
    """
    import zenoh

    @dataclass
    class WithServer(SyncableObject):
        @remote_method()
        def get_value(self) -> int:
            return 42

        @get_value.server()
        def get_value(self, query: zenoh.Query):
            registry = self.synq_session.type_registry
            query.reply(query.key_expr, payload=registry.dumps(99), encoding=zenoh.Encoding.APPLICATION_YAML)

    obj = WithServer("test/rpc_server_hook", synq_authoritive=True)
    session_b = Session()
    remote = WithServer.from_topic(obj.synq_absolute_path, session=session_b)

    assert remote.get_value() == 99


def test_server_hook_not_called_on_local_call():
    """
    @method.server() should NOT be invoked when the method is called directly
    on the authoritative instance — local calls bypass the zenoh callback entirely.
    """
    import zenoh

    hook_called = {"n": 0}

    @dataclass
    class WithServerLocal(SyncableObject):
        @remote_method()
        def get_value(self) -> int:
            return 42

        @get_value.server()
        def get_value(self, query: zenoh.Query):
            hook_called["n"] += 1
            registry = self.synq_session.type_registry
            query.reply(query.key_expr, payload=registry.dumps(99), encoding=zenoh.Encoding.APPLICATION_YAML)

    obj = WithServerLocal("test/rpc_server_local", synq_authoritive=True)
    result = obj.get_value()
    assert result == 42
    assert hook_called["n"] == 0


def test_server_hook_reply_err_raises_rpc_exception():
    """
    When @method.server() calls query.reply_err(), the caller should receive
    an RpcException.
    """
    import zenoh

    @dataclass
    class WithServerErr(SyncableObject):
        @remote_method()
        def do_thing(self) -> str:
            return "ok"

        @do_thing.server()
        def do_thing(self, query: zenoh.Query):
            query.reply_err("not allowed")

    obj = WithServerErr("test/rpc_server_err", synq_authoritive=True)
    session_b = Session()
    remote = WithServerErr.from_topic(obj.synq_absolute_path, session=session_b)

    with pytest.raises(RpcException):
        remote.do_thing()


def test_server_hook_exception_forwarded_as_error():
    """
    An uncaught exception in @method.server() should be caught by SpiriSynq
    and forwarded as reply_err, raising RpcException on the caller.
    """
    import zenoh

    @dataclass
    class WithServerRaise(SyncableObject):
        @remote_method()
        def do_thing(self) -> str:
            return "ok"

        @do_thing.server()
        def do_thing(self, query: zenoh.Query):
            raise RuntimeError("boom")

    obj = WithServerRaise("test/rpc_server_raise", synq_authoritive=True)
    session_b = Session()
    remote = WithServerRaise.from_topic(obj.synq_absolute_path, session=session_b)

    with pytest.raises(RpcException):
        remote.do_thing()


def test_server_hook_receives_self_and_params():
    """
    The server hook receives the authoritative instance as self and the raw
    query, so it can decode params and use instance state in its reply.
    """
    import zenoh

    @dataclass
    class WithServerSelf(SyncableObject):
        multiplier: int = 5

        @remote_method()
        def scale(self, x: int) -> int:
            return x * self.multiplier

        @scale.server()
        def scale(self, query: zenoh.Query):
            registry = self.synq_session.type_registry
            params = {k: registry.load(v) for k, v in dict(query.parameters).items()}
            result = params["x"] * self.multiplier * 2
            query.reply(query.key_expr, payload=registry.dumps(result), encoding=zenoh.Encoding.APPLICATION_YAML)

    obj = WithServerSelf("test/rpc_server_self", synq_authoritive=True, multiplier=5)
    session_b = Session()
    remote = WithServerSelf.from_topic(obj.synq_absolute_path, session=session_b)

    assert remote.scale(3) == 30  # 3 * 5 * 2


def test_timeout_chaining():
    """
    .timeout(n).sync(args) and .timeout(n).as_async(args) should work
    and not affect calls without a timeout.
    """
    @dataclass
    class WithTimeout(SyncableObject):
        @remote_method()
        def echo(self, x: int) -> int:
            return x

    obj = WithTimeout("test/rpc_timeout", synq_authoritive=True)
    session_b = Session()
    remote = WithTimeout.from_topic(obj.synq_absolute_path, session=session_b)

    assert remote.echo.timeout(5).sync(99) == 99
    assert remote.echo(99) == 99  # baseline unaffected
