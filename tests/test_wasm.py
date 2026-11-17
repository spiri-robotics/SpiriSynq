"""`@wasm.*`: code that travels with the type, run in WebAssembly by the core."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from SpiriSynq import on_change, wasm
from SpiriSynq.remote_callables import remote_method
from SpiriSynq.syncable_objects import Sample, SyncableObject

from .conftest import wait_for

WAT_PERCENT = """
(module
  (func (export "doubled") (result i32) (i32.const 0))
)
"""


@dataclass
class Battery(SyncableObject):
    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000005:1"
    charge_mah: float = 2500.0
    capacity_mah: float = 5000.0
    current_a: float = 0.0
    smoothed_current_a: float = 0.0
    limit: int = 100
    serial: str = "BAT-2024-00172"
    code: str = ""
    cells: dict[int, float] = field(default_factory=dict)

    @property
    @wasm.python
    def percent(self) -> float:
        return 100 * self.charge_mah / self.capacity_mah

    @wasm.micropython
    def minutes_left(self, draw_a: float) -> float:
        return (self.charge_mah / 1000) / draw_a * 60

    @wasm.python
    def set_limit(self, percent: int) -> None:
        self.limit = max(50, min(percent, 100))

    @wasm.python
    async def year(self) -> int:
        import re

        m = re.match(r"BAT-(\d+)-\d+$", self.serial)
        return int(m.group(1))

    @wasm.python
    def weakest(self, count: int) -> dict[int, float]:
        ranked = sorted(self.cells.items(), key=lambda c: c[1])
        return {i: v for i, v in ranked[:count]}

    @property
    @wasm.lua("""
        function lua_percent(self)
          return 100 * self.charge_mah / self.capacity_mah
        end
    """)
    def lua_percent(self) -> float: ...

    @property
    @wasm.javascript("""
        function js_percent(self) {
          return 100 * self.charge_mah / self.capacity_mah;
        }
    """)
    def js_percent(self) -> float: ...

    @wasm.wat(WAT_PERCENT, export="doubled")
    def nothing(self) -> None: ...

    @on_change.mirror("current_a")
    @wasm.lua("""
        function smooth(self, value)
          self.smoothed_current_a = 0.5 * self.smoothed_current_a + 0.5 * value
        end
    """)
    def smooth(self, value: float) -> None: ...

    @wasm.python
    def code_before_send(self, value, sample):
        sample.send(value.encode(), encoding="text/plain")

    @wasm.python
    def code_after_receive(self, sample):
        return sample.payload.decode().upper()

    @remote_method
    def history(self, minutes: int) -> list[float]:
        return [12.0] * minutes

    @history.after_call
    @wasm.python
    async def history(self, replies) -> AsyncIterator[list[float]]:
        async for reply in replies:
            yield [v + 1 for v in reply.decode()]

    @wasm.python
    def broken(self) -> int:
        raise ValueError("no")


def test_properties_and_methods_run_in_wasm(other):
    battery = Battery("battery", synq_authoritive=True, cells={1: 3.7, 2: 3.5, 3: 3.9})
    assert battery.percent == 50.0
    assert battery.lua_percent == 50.0
    assert battery.js_percent == 50.0
    assert battery.minutes_left(draw_a=10.0) == 15.0
    assert battery.weakest(2) == {2: 3.5, 1: 3.7}
    assert asyncio.run(battery.year()) == 2024
    assert battery.nothing() is None
    battery.set_limit(10)
    assert battery.limit == 50
    mirror = Battery.from_topic("host/battery", session=other)
    assert mirror.percent == 50.0
    mirror.set_limit(70)  # a mirror's write, to the owner
    assert mirror.limit == 70
    wait_for(lambda: battery.limit == 70)
    with pytest.raises(RuntimeError, match="ValueError: no"):
        battery.broken()
    assert Battery.percent.fget.wasm_runtime == "micropython"
    assert Battery.lua_percent.fget.wasm_runtime == "lua"
    assert Battery.js_percent.fget.wasm_runtime == "quickjs"
    assert Battery.nothing.wasm_runtime == "wat"
    assert "micropython" in repr(Battery.minutes_left)


def test_hooks_run_in_wasm(other):
    battery = Battery("battery", synq_authoritive=True, code="abc")
    mirror = Battery.from_topic("host/battery", session=other)
    assert mirror.code == "ABC"
    battery.current_a = 10.0
    wait_for(lambda: mirror.smoothed_current_a == 5.0)
    assert mirror.history(2) == [13.0, 13.0]


def test_the_schema_carries_the_code():
    code = Battery.synq_schema()["x-wasm"]
    assert code["percent"]["role"] == "property"
    assert code["smooth"] == {**code["smooth"], "role": "on_change_mirror", "of": "current_a"}
    assert code["code_before_send"]["role"] == "before_send"
    assert code["history.after_call"]["of"] == "history"
    assert code["minutes_left"]["params"]["properties"] == {"draw_a": {"type": "number"}}
    assert "async" not in code["year"]["source"]
    assert ":" not in code["minutes_left"]["source"].splitlines()[0].split(")")[0]


def test_wasm_code_must_stand_alone():
    limit = 3

    with pytest.raises(TypeError, match="uses limit from around it"):

        @wasm.python
        def uses_closure(self) -> int:
            return limit

    not_a_def = lambda self: 1  # noqa: E731
    with pytest.raises(TypeError, match="only a function"):
        wasm.python(not_a_def)
    with pytest.raises(TypeError, match="can't read its source"):
        wasm.python(type("NoSource", (), {}))


def test_not_in_this_release():
    with pytest.raises(NotImplementedError, match="mamba"):
        wasm.mamba(lambda self: 1)
    with pytest.raises(NotImplementedError, match="asm"):
        wasm.asm(lambda self: 1)

    @dataclass
    class OwnerCode(SyncableObject):
        synq_type_id = "1a2b3c4d-0000-4000-8000-000000000006:1"

        @remote_method
        @wasm.python
        def run(self) -> int:
            return 1

    with pytest.raises(NotImplementedError, match="WASM remote methods"):
        OwnerCode.synq_schema()

    @dataclass
    class OwnerCallback(SyncableObject):
        synq_type_id = "1a2b3c4d-0000-4000-8000-000000000007:1"
        x: int = 0

        @on_change.owner("x")
        @wasm.python
        def changed(self, value):
            pass

    with pytest.raises(NotImplementedError, match="on_change.owner"):
        OwnerCallback.synq_schema()


def test_code_from_a_file(tmp_path):
    with pytest.raises(TypeError, match="not both"):
        wasm.lua("x", file="y")
    with pytest.raises(TypeError, match="not both"):
        wasm.lua()
    path = tmp_path / "f.lua"
    path.write_text("function f(self) return 1 end")
    stub = _stub_in(tmp_path)
    assert wasm.lua(file="f.lua")(stub).source == "function f(self) return 1 end"
    (tmp_path / "m.wasm").write_bytes(b"\0asm\1\0\0\0")
    made = wasm.module(file="m.wasm", export="go")(stub)
    assert (made.source, made.entry, made.wasm_runtime) == (b"\0asm\1\0\0\0", "go", "module")
    assert made.hash.startswith("sha256:")


def _stub_in(directory):
    namespace: dict = {}
    exec(compile("def f(self): ...", str(directory / "stub.py"), "exec"), namespace)
    return namespace["f"]


def test_a_sample_out_of_place():
    with pytest.raises(RuntimeError, match="only a before_send"):
        Sample().send(b"x")


def test_async_python_becomes_blocking():
    @wasm.python
    async def body(self, replies) -> int:
        total: int = 0
        later: int  # noqa: F842 - an annotation alone, which the blocking form drops
        async with replies as r:
            async for reply in r:
                total += await reply
        return total

    source = body.source
    assert "async" not in source and "await" not in source
    assert "total = 0" in source and "later" not in source
