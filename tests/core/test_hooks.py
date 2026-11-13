"""WASM field and caller hooks through SpiriSynq._core: they run by themselves, in MicroPython."""

import hashlib
import json

import pytest

from SpiriSynq import _core

from .conftest import BATTERY, config, mirror, wait_for

TOPIC = "robot/battery"

HOOKS = {
    "smooth": (
        "on_change_mirror",
        "voltage",
        "def smooth(self, value):\n    self.smoothed = value / 2\n",
    ),
    "enc": (
        "before_send",
        "code",
        "def enc(self, value, sample):\n    sample.send(value.encode(), encoding='text/plain')\n",
    ),
    "dec": (
        "after_receive",
        "code",
        "def dec(self, sample):\n    return sample.payload.decode().upper()\n",
    ),
    "refuse": (
        "after_receive",
        "broken",
        "def refuse(self, sample):\n    raise ValueError('not today')\n",
    ),
    "check": (
        "before_call",
        "set_limit",
        "def check(self, call, percent):\n"
        "    if percent > 100:\n"
        "        raise ValueError('too much')\n"
        "    call.send_args({'percent': percent})\n",
    ),
    "doubled": (
        "after_call",
        "set_limit",
        "def doubled(self, replies):\n"
        "    for reply in replies:\n"
        "        yield reply.decode() * 2\n",
    ),
    "tally": (
        "after_call",
        "count",
        "def tally(self, replies):\n"
        "    total = 0\n"
        "    for reply in replies:\n"
        "        total += reply.decode()\n"
        "        yield total\n",
    ),
    "upper": (
        "after_call",
        "read",
        "def upper(self, replies):\n"
        "    for reply in replies:\n"
        "        yield reply.decode().upper()\n",
    ),
    "oops": (
        "after_call",
        "blob",
        "def oops(self, replies):\n"
        "    for reply in replies:\n"
        "        pass\n"
        "    raise ValueError('oops')\n"
        "    yield 1\n",
    ),
}


def entry(name: str, role: str, of: str, source: str) -> dict:
    code = {
        "role": role,
        "loader": "python",
        "entry": name,
        "source": source,
        "hash": f"sha256:{hashlib.sha256(source.encode()).hexdigest()}",
        "of": of,
    }
    if role == "before_call":
        code["params"] = {"properties": {"percent": {}}}
    return code


@pytest.fixture
def battery() -> _core.Schema:
    schema = json.loads(json.dumps(BATTERY))
    schema["properties"] |= {
        "smoothed": {"type": "number"},
        "code": {"type": "string"},
        "broken": {"type": "string"},
    }
    schema["x-remote-methods"]["count"] = {"x-generator": True, "returns": {"type": "integer"}}
    schema["x-wasm"] = {name: entry(name, *hook) for name, hook in HOOKS.items()}
    return _core.Schema(schema)


def answered(node: _core.Node, request: _core.RequestId) -> tuple[list, str]:
    """The replies to `request`, as values or `(kind, message)` errors, and how it ended."""
    replies = []
    while True:
        notice = wait_for(node, (_core.Reply, _core.RequestEnded), lambda n: n.request == request)
        if isinstance(notice, _core.RequestEnded):
            return replies, notice.end
        replies.append(notice.value if notice.error is None else notice.error)


def called(node: _core.Node, method: str) -> _core.Call:
    return wait_for(node, _core.Call, lambda n: n.method == method)


def test_a_mirrors_on_change_runs_when_its_value_changes(network, battery):
    a, b = network(2)
    owner = a.create_owner(TOPIC, battery)
    a.set_field(owner, "voltage", 12.0)
    copy = b.create_mirror(TOPIC, battery)
    got = wait_for(b, _core.Incoming, lambda n: n.path == "voltage")
    assert b.set_field_local(copy, "voltage", got.data)
    assert b.field(copy, "smoothed") == 6.0
    b.set_field_local(copy, "smoothed", 1.0)
    assert not b.set_field_local(copy, "voltage", got.data)
    assert b.field(copy, "smoothed") == 1.0
    assert a.field(owner, "smoothed") is None


def test_fields_go_out_and_come_in_through_their_hooks(network, battery):
    a, b = network(2)
    owner = a.create_owner(TOPIC, battery)
    copy = b.create_mirror(TOPIC, battery)
    wait_for(b, _core.Rehydrated)
    assert a.set_field(owner, "code", "abc")
    assert a.set_field(owner, "code", "abc")  # Sent every time: the engine can't compare.
    got = wait_for(b, _core.Incoming, lambda n: n.path == "code")
    assert (got.data, got.origin) == ("ABC", "owner")
    assert b.set_field(copy, "code", "xyz")
    got = wait_for(a, _core.Incoming, lambda n: n.path == "code")
    assert (got.data, got.origin) == ("XYZ", "write")


def test_a_sample_its_hook_cant_read_is_bad_data(network, battery):
    a, b = network(2)
    owner = a.create_owner(TOPIC, battery)
    b.create_mirror(TOPIC, battery)
    wait_for(b, _core.Rehydrated)
    a.send_raw(owner, "broken", _core.Sample(b"x", "text/plain"))
    got = wait_for(b, _core.BadData)
    assert got.path == "broken"
    assert "ValueError: not today" in got.reason


def test_before_call_checks_and_after_call_reads_the_replies(network, battery):
    a, b = network(2)
    a.create_owner(TOPIC, battery)
    copy = mirror(b, TOPIC, battery)
    with pytest.raises(_core.SynqError, match="too much"):
        b.call(copy, "set_limit", {"percent": 200})
    request = b.call(copy, "set_limit", {"percent": 50}, raw_replies=True)
    call = called(a, "set_limit")
    assert call.args == {"percent": 50}
    a.reply(call.query, 7)
    a.finish(call.query)
    assert answered(b, request) == ([14], "done")


def test_an_error_reply_the_after_call_lets_through_reaches_the_caller(network, battery):
    a, b = network(2)
    a.create_owner(TOPIC, battery)
    copy = mirror(b, TOPIC, battery)
    request = b.call(copy, "set_limit", {"percent": 10})
    a.reply_error(called(a, "set_limit").query, "LimitError", "the motor is hot")
    assert answered(b, request) == ([("LimitError", "the motor is hot")], "failed")


def test_a_generators_results_come_as_theyre_yielded(network, battery):
    a, b = network(2)
    a.create_owner(TOPIC, battery)
    copy = mirror(b, TOPIC, battery)
    request = b.call(copy, "count")
    query = called(a, "count").query
    for n in (1, 2, 3):
        a.reply(query, n)
    a.finish(query)
    assert answered(b, request) == ([1, 3, 6], "done")


def test_a_raw_call_runs_the_after_call(network, battery):
    a, b = network(2)
    a.create_owner(TOPIC, battery)
    copy = mirror(b, TOPIC, battery)
    request = b.call_raw(copy, "read", {"path": "logs/today"})
    call = called(a, "read")
    a.reply(call.query, "all quiet")
    a.finish(call.query)
    assert answered(b, request) == (["ALL QUIET"], "done")


def test_an_after_call_that_fails_fails_the_call(network, battery):
    a, b = network(2)
    a.create_owner(TOPIC, battery)
    copy = mirror(b, TOPIC, battery)
    request = b.call(copy, "blob")
    call = called(a, "blob")
    a.reply(call.query, 1)
    a.finish(call.query)
    [(kind, message)], end = answered(b, request)
    assert (kind, end) == ("SynqError", "failed")
    assert "ValueError: oops" in message


def test_closing_ends_the_after_calls_still_waiting(network, battery):
    a, b = network(2)
    a.create_owner(TOPIC, battery)
    copy = mirror(b, TOPIC, battery)
    b.call(copy, "count")
    called(a, "count")
    b.close()
    assert b.next_notice(0.1) is None


SPIN = "def spin(self):\n    while True:\n        pass\n"


def test_wasm_limits_are_set_per_node(battery):
    with pytest.raises(ValueError):
        _core.Node(config(), wasm_timeout=-1.0)
    schema = json.loads(json.dumps(BATTERY))
    spin = entry("spin", "property", "", SPIN)
    del spin["of"]
    schema["x-wasm"] = {"spin": spin}
    node = _core.Node(config(), wasm_timeout=0.2, wasm_memory_mib=32)
    try:
        owner = node.create_owner(TOPIC, _core.Schema(schema))
        with pytest.raises(_core.SynqError, match="time"):
            node.run_wasm(owner, "spin")
    finally:
        node.close()
