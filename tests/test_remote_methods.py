"""Remote methods: calls from mirrors, streams, errors, key templates, hooks and raw methods."""

import asyncio
import struct
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import pytest

from SpiriSynq import _core
from SpiriSynq.remote_callables import (
    Call,
    Query,
    Reply,
    RpcException,
    format_parameters,
    parse_parameters,
    remote_method,
    remote_method_raw,
)
from SpiriSynq.syncable_objects import SyncableObject


@dataclass(frozen=True)
class Snapshot:
    taken_at: float
    jpeg: bytes


@dataclass
class Battery(SyncableObject):
    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000001:1"
    limit: int = 100
    history: list[float] = field(default_factory=lambda: [12.0, 12.1, 12.2])

    @remote_method
    def set_limit(self, percent: int) -> int:
        """Clamp and set the charge limit."""
        self.limit = max(50, min(percent, 100))
        return self.limit

    @remote_method()
    def count_down(self, start: int = 3) -> Iterator[int]:
        yield from range(start, -1, -1)

    @remote_method
    async def slow_double(self, x: int) -> int:
        await asyncio.sleep(0.01)
        return 2 * x

    @remote_method
    async def ticks(self, n: int) -> AsyncIterator[int]:
        for i in range(n):
            await asyncio.sleep(0)
            yield i

    @remote_method
    def fail(self) -> None:
        raise PermissionError("not you")

    @remote_method(timeout=0.3)
    def sleepy(self) -> int:
        time.sleep(1.0)
        return 1

    @remote_method(key="read/data/{path:**}")
    def read(self, path: str, upper: bool = False) -> str:
        return path.upper() if upper else path

    @remote_method
    def whoami(self, query: Query | None) -> str:
        return "owner" if query is None else query.parameters.get("token", "")

    @remote_method
    def history_since(self, minutes: int) -> list[float]:
        return self.history[-minutes:]

    @history_since.before_call
    async def history_since(self, call: Call, minutes: int) -> None:
        if not 1 <= minutes <= 60:
            raise ValueError("minutes must be between 1 and 60")
        await call.send_args({"minutes": minutes})

    @history_since.after_call
    async def history_since(self, replies) -> AsyncIterator[list[float]]:
        async for reply in replies:
            yield [round(v * 10) for v in reply.decode()]

    @remote_method_raw
    async def snapshot(self, query: Query, quality: int = 90) -> AsyncIterator[Reply]:
        attachment = struct.pack("<d", 1.5)
        for chunk in (b"\xff\xd8", bytes([quality]), b"\xff\xd9"):
            yield Reply(chunk, encoding="image/jpeg", attachment=attachment)
            attachment = None

    @snapshot.after_call
    async def snapshot(self, replies) -> AsyncIterator[Snapshot]:
        taken_at, jpeg = None, b""
        async for reply in replies:
            if reply.attachment:
                (taken_at,) = struct.unpack("<d", reply.attachment)
            jpeg += reply.payload
        yield Snapshot(taken_at, jpeg)

    @remote_method_raw(key="write/{path:**}")
    async def write(self, query: Query, path: str) -> AsyncIterator[Reply]:
        (offset,) = struct.unpack("<Q", query.attachment)
        yield Reply.encode(f"{path}@{offset}:{query.payload.decode()}:{query.parameters}")

    @write.before_call
    async def write(self, call: Call, path: str, offset: int, data: bytes) -> None:
        await call.send(
            data, encoding="text/plain", attachment=struct.pack("<Q", offset), parameters={"a": "1"}
        )

    @remote_method_raw(generator=True)
    async def chunks(self, query: Query, n: int) -> AsyncIterator[Reply]:
        for i in range(n):
            yield Reply(bytes([i]))

    @remote_method
    def silent(self, x: int) -> int:
        return x

    @silent.before_call
    async def silent(self, call: Call, x: int) -> None:
        if x == 2:
            await call.send_args({"x": x})
            await call.send_args({"x": x})

    @remote_method_raw
    async def not_replies(self, query: Query) -> AsyncIterator[Reply]:
        yield "nope"

    @remote_method
    def two(self) -> Iterator[int]:
        yield 1

    @remote_method
    def wrong_type(self) -> int:
        return "text"

    @remote_method
    def doubled(self, yield_none: bool = False) -> int:
        return 0 if yield_none else 1

    @doubled.after_call
    async def doubled(self, replies) -> AsyncIterator[int]:
        async for reply in replies:
            if not reply.decode():
                continue
            yield 1
            yield 2

    @remote_method
    def fails_hooked(self) -> int:
        raise PermissionError("no")

    @fails_hooked.after_call
    async def fails_hooked(self, replies) -> AsyncIterator[int]:
        async for reply in replies:
            yield reply.decode()


@pytest.fixture
def pair(other):
    battery = Battery("battery", synq_authoritive=True)
    mirror = Battery.from_topic("host/battery", session=other)
    yield battery, mirror


def test_a_call_runs_on_the_owner(pair):
    battery, mirror = pair
    assert mirror.set_limit(30) == 50
    assert battery.limit == 50
    assert mirror.set_limit(percent=80) == 80


def test_generators_stream_their_values(pair):
    _, mirror = pair
    assert list(mirror.count_down(start=2)) == [2, 1, 0]
    assert list(mirror.ticks(3)) == [0, 1, 2]
    assert mirror.slow_double(4) == 8


def test_async_calls(pair):
    battery, mirror = pair

    async def main():
        assert await mirror.set_limit.as_async(70) == 70
        assert [v async for v in mirror.count_down.as_async(start=1)] == [1, 0]
        assert await mirror.history_since.as_async(1) == [122]
        # On the owner, as_async runs it directly.
        assert await battery.slow_double.as_async(2) == 4
        assert await battery.set_limit.as_async(60) == 60
        assert [v async for v in battery.count_down.as_async(1)] == [1, 0]
        assert [v async for v in battery.ticks.as_async(2)] == [0, 1]

    asyncio.run(main())


def test_errors_reach_the_caller(pair):
    _, mirror = pair
    with pytest.raises(RpcException) as raised:
        mirror.fail()
    assert (raised.value.kind, raised.value.message) == ("PermissionError", "not you")
    assert str(raised.value) == "[host/battery] PermissionError: not you"
    assert str(RpcException("Plain")) == "Plain"
    with pytest.raises(TimeoutError):
        mirror.sleepy()
    with pytest.raises(RpcException, match="TypeError"):
        mirror.set_limit("high")
    with pytest.raises(RpcException, match="BadValue"):
        mirror.wrong_type()
    with pytest.raises(RuntimeError, match="expected one result, got 2"):
        mirror.doubled()
    with pytest.raises(RpcException, match="PermissionError"):
        mirror.fails_hooked()

    async def nothing():
        return await mirror.doubled.as_async(yield_none=True)

    with pytest.raises(RuntimeError, match="expected one result, got 0"):
        asyncio.run(nothing())


def test_key_templates_put_arguments_in_the_key(pair):
    _, mirror = pair
    assert mirror.read("logs/today.txt") == "logs/today.txt"
    assert mirror.read("a*b", upper=True) == "A*B"
    with pytest.raises(Exception, match="empty"):
        mirror.read("logs//x")


def test_a_method_can_see_its_query(pair):
    battery, mirror = pair
    assert battery.whoami() == "owner"
    assert mirror.whoami() == ""
    request = mirror.synq_session.node.call(mirror._synq_id, "whoami", parameters="token=abc")
    assert [v for v, _ in mirror.synq_session.replies(request, 5)] == ["abc"]
    assert parse_parameters("a=1;b=;c") == {"a": "1", "b": "", "c": ""}
    assert format_parameters(None) == "" and format_parameters("x=1") == "x=1"


def test_caller_hooks(pair):
    _, mirror = pair
    assert mirror.history_since(2) == [121, 122]
    with pytest.raises(ValueError, match="between 1 and 60"):
        mirror.history_since(600)
    with pytest.raises(RuntimeError, match="without sending"):
        mirror.silent(1)
    with pytest.raises(RuntimeError, match="once"):
        mirror.silent(2)


def test_raw_methods(pair):
    battery, mirror = pair
    assert mirror.snapshot(quality=75) == Snapshot(1.5, b"\xff\xd8K\xff\xd9")
    assert mirror.write("logs/a", 7, b"hi") == "logs/a@7:hi:{'a': '1'}"
    assert list(mirror.chunks(n=3)) == [b"\x00", b"\x01", b"\x02"]
    with pytest.raises(RpcException, match="yields Replies"):
        mirror.not_replies()
    with pytest.raises(TypeError, match="raw remote method"):
        battery.snapshot()
    with pytest.raises(TypeError, match="raw remote method"):
        battery.snapshot.as_async()
    with pytest.raises(TypeError, match="async generator"):
        remote_method_raw(lambda self, q: None)


def test_a_mirror_without_the_hooks_gets_payloads(pair, open_session):
    battery, _ = pair
    third = open_session("third")
    untyped = third.from_topic_untyped("host/battery")
    assert untyped.synq_call("count_down", start=1) == [1, 0]
    assert untyped.synq_call("chunks", n=2) == [b"\x00", b"\x01"]
    with pytest.raises(RpcException, match="PermissionError"):
        untyped.synq_call("fail")


def test_the_owner_calls_its_own_methods_directly(pair):
    battery, _ = pair
    assert battery.set_limit(10) == 50
    assert list(battery.count_down(1)) == [1, 0]
    assert battery.slow_double(3) == 6
    assert Battery.set_limit(battery, 90) == 90


def test_pipe_isnt_in_this_release(pair):
    _, mirror = pair
    with pytest.raises(NotImplementedError):
        mirror.set_limit.pipe(1)


def test_replies(pair):
    reply = Reply.encode([1, 2])
    assert reply.decode() == [1, 2]
    assert "application/yaml" in repr(reply)
    with pytest.raises(ValueError, match="usual way"):
        Reply(b"x").decode()


def test_calls_for_methods_that_arent_there(pair):
    battery, mirror = pair
    node = battery.synq_session.node
    # An owner whose class lost the method still answers, with an error.
    del Battery._synq_member_cache
    methods, callbacks = Battery._synq_member_map()
    Battery._synq_member_cache = ({}, callbacks)
    try:
        with pytest.raises(RpcException, match="LookupError"):
            mirror.set_limit(70)
    finally:
        Battery._synq_member_cache = (methods, callbacks)
    assert node is not None


def test_a_call_to_a_closed_owner_fails(pair):
    battery, mirror = pair
    battery.close()
    with pytest.raises((TimeoutError, RpcException)):
        mirror.set_limit(70)
    _ = _core


def test_the_owner_passes_no_query_to_itself(pair):
    battery, _ = pair
    assert asyncio.run(battery.whoami.as_async()) == "owner"
