"""zenoh without the engine: puts, watches, gets and scouting, as the command line uses them."""

import json
import socket
import threading
import time

import pytest

from SpiriSynq import _core

from .conftest import BATTERY, config, wait_for, wait_until


def test_put_and_watch(network):
    a, b = network(2)
    watch = b.watch("raw/**")
    assert watch.recv(0.05) is None
    # A put only reaches a subscriber once zenoh has routed it there: put until one arrives.
    got = None
    while got is None:
        a.put("raw/x", b"1", attachment=b"note")
        got = watch.recv(0.1)
    assert (got.key, got.payload, got.encoding, got.ok) == ("raw/x", b"1", "application/yaml", True)
    assert got.attachment == b"note"
    assert got.timestamp is None  # peers don't stamp samples unless told to
    a.put("raw/y", b"\x00\xff", encoding="zenoh/bytes")
    got = watch.recv(5)
    while got.key != "raw/y":
        got = watch.recv(5)
    assert (got.payload, got.encoding, got.attachment) == (b"\x00\xff", "zenoh/bytes", None)
    watch.close()
    watch.close()
    with pytest.raises(_core.SynqError, match="closed"):
        watch.recv(0.1)


def test_a_watch_ends_when_its_node_closes(network):
    (a,) = network(1)
    watch = a.watch("raw/**")
    a.close()
    with pytest.raises(_core.SynqError, match="closed"):
        watch.recv()
    with pytest.raises(_core.SynqError):
        a.watch("raw/**")
    with pytest.raises(_core.SynqError):
        a.put("raw/x", b"1")
    with pytest.raises(_core.SynqError):
        a.get("raw/x")
    with pytest.raises(_core.SynqError):
        a.routers()


def test_timestamps():
    stamped = json.loads(config()) | {"timestamping": {"enabled": True}}
    a = _core.Node(json.dumps(stamped))
    try:
        watch = a.watch("raw/**")
        a.put("raw/t", b"1")
        got = watch.recv(5)
        assert got.timestamp is not None and abs(got.timestamp - time.time()) < 60
    finally:
        a.close()


def reachable(caller: _core.Node, topic: str) -> None:
    """Wait until `caller` has learned of the object at `topic`: until it answers a query."""
    wait_until(lambda: any(r.ok for r in caller.get(f"{topic}/sr_metadata", timeout=0.5)))


def test_get_from_an_object(network):
    owner, caller = network(2)
    obj = owner.create_owner("raw/battery", _core.Schema(BATTERY))
    reachable(caller, "raw/battery")
    owner.set_field(obj, "voltage", 12.5)
    replies = list(caller.get("raw/battery/sr_rehydrate", timeout=5))
    assert [(r.key, r.payload, r.ok) for r in replies if r.encoding == "application/yaml"] == [
        ("raw/battery/voltage", b"12.5\n", True)
    ]
    assert replies[-1].encoding == "x-spirisynq/done"
    # Nothing answers in time: the replies end at the timeout.
    replies = caller.get("raw/nobody", timeout=0.3)
    assert iter(replies) is replies
    assert list(replies) == []


def test_error_replies(network):
    owner, caller = network(2)
    obj = owner.create_owner("raw/battery", _core.Schema(BATTERY))
    reachable(caller, "raw/battery")
    # A call: its arguments are the payload. The owner answers with an error.
    replies = caller.get("raw/battery/set_limit", payload=b"{percent: 5}", timeout=5)
    call = wait_for(owner, _core.Call)
    assert call.args == {"percent": 5}
    owner.reply_error(call.query, "ValueError", "too low")
    got = next(replies)
    assert not got.ok and got.key == "" and got.attachment is None
    assert _core.yaml_load(got.payload) == {"type": "ValueError", "message": "too low"}
    assert got.timestamp is None
    _ = obj


def test_get_rejects_a_bad_timeout(network):
    (a,) = network(1)
    with pytest.raises(ValueError):
        a.get("raw/x", timeout=-1)
    with pytest.raises(ValueError):
        _core.scout("peer", -1)


def test_routers(network):
    (a,) = network(1)
    assert a.routers() == []


def _hello(zid: bytes, locators: list[str]) -> bytes:
    """A zenoh Hello, as a peer sends one back to a scout: header with the locators flag,
    protocol version, peer kind and id length, the id, then the locators."""
    out = bytes([0x02 | 0x20, 0x09, 0x01 | ((len(zid) - 1) << 4)]) + zid
    out += bytes([len(locators)])
    for locator in locators:
        out += bytes([len(locator)]) + locator.encode()
    return out


def test_scout_finds_what_answers():
    """A fake node on loopback answers the scout, over unicast UDP: no multicast."""
    fake = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fake.bind(("127.0.0.1", 0))
    fake.settimeout(5)
    port = fake.getsockname()[1]
    zid = bytes(range(1, 17))

    def answer():
        try:
            _, sender = fake.recvfrom(1024)
            fake.sendto(_hello(zid, ["tcp/127.0.0.1:7447"]), sender)
        except OSError:
            pass

    thread = threading.Thread(target=answer)
    thread.start()
    config = {"scouting": {"multicast": {"address": f"127.0.0.1:{port}", "interface": "127.0.0.1"}}}
    found = _core.scout("peer|router", 1.0, json.dumps(config))
    thread.join()
    fake.close()
    assert len(found) == 1
    found_zid, kind, locators = found[0]
    assert kind == "peer" and locators == ["tcp/127.0.0.1:7447"]
    assert len(found_zid) == 32


def test_scout_with_nothing_to_find():
    config = {"scouting": {"multicast": {"address": "127.0.0.1:9", "interface": "127.0.0.1"}}}
    assert _core.scout("router", 0.2, json.dumps(config)) == []
    with pytest.raises(ValueError, match="can't scout for `robot`"):
        _core.scout("robot", 0.1)
    with pytest.raises(_core.SynqError):
        _core.scout("peer", 0.1, "{nonsense")


def test_waiting_lets_signals_through(network):
    """A Ctrl-C while waiting raises in the waiting thread."""
    import _thread

    (a,) = network(1)
    watch = a.watch("raw/**")
    timer = threading.Timer(0.3, _thread.interrupt_main)
    timer.start()
    with pytest.raises(KeyboardInterrupt):
        watch.recv()
    timer.cancel()
    wait_until(lambda: True)


def test_a_call_nobody_answers_ends_at_the_timeout(network):
    """zenoh's own timeout reply isn't passed on: the replies just end."""
    owner, caller = network(2)
    obj = owner.create_owner("raw/battery", _core.Schema(BATTERY))
    reachable(caller, "raw/battery")
    assert list(caller.get("raw/battery/set_limit", payload=b"{percent: 5}", timeout=0.3)) == []
    _ = obj
