"""The `synq` command line. Tests assert on what it prints: commands exit 0 even when nothing
answers."""

import _thread
import json
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from io import StringIO
from types import SimpleNamespace

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

import SpiriSynq.cli as cli
from SpiriSynq import _core
from SpiriSynq.remote_callables import (
    Query,
    Reply,
    RpcException,
    remote_method,
    remote_method_raw,
)
from SpiriSynq.syncable_objects import SyncableObject

from .conftest import wait_for
from .core.conftest import BATTERY, config

runner = CliRunner()


@dataclass
class Rover(SyncableObject):
    synq_type_id = "urn:pypi:spiri-rover:1.0:spirirover.Rover"
    speed: float = 1.0
    name: str = "rover"
    picture: bytes = b""

    @remote_method
    def drive(self, distance: float, fast: bool = False) -> float:
        """Drive forward."""
        if distance < 0:
            raise RpcException("ValueError", "can't drive backwards")
        return distance * (2 if fast else 1)

    @remote_method
    def countdown(self, start: int) -> Iterator[int]:
        yield from range(start, 0, -1)

    @remote_method_raw
    async def snapshot(self, query: Query) -> AsyncIterator[Reply]:
        yield Reply(b"\x89PNG", encoding="image/png")


@dataclass
class Beacon(SyncableObject):
    synq_type_id = "5b2c0e8e-0000-4000-8000-0000000000c1:1"
    on: bool = False


def invoke(*args: str, input: str | None = None, truncate: bool = False):
    """Run the CLI: its exit code, stdout and stderr. It prints through its own consoles, which
    the runner doesn't capture, so they're swapped for the call."""
    out, err = StringIO(), StringIO()
    original = cli.console_out, cli.console_err
    cli.console_out = Console(file=out, highlight=False, soft_wrap=not truncate, width=60)
    cli.console_err = Console(file=err, highlight=False, soft_wrap=True)
    try:
        result = runner.invoke(cli.app, list(args), input=input)
    finally:
        cli.console_out, cli.console_err = original
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, out.getvalue(), err.getvalue()


def documents(text: str) -> list:
    return [d for d in yaml.safe_load_all(text) if d is not None]


@pytest.fixture
def rover():
    rover = Rover("rover", synq_authoritive=True)
    wait_for(
        lambda: any(m["topic"] == "host/rover" for m in cli.session().list_topics(timeout=0.5))
    )
    return rover


class Putter:
    """Puts on a key over and over from a thread, until stopped: a watch only sees puts made
    after zenoh routed them to it."""

    def __init__(self, key: str, payload: bytes, **options):
        self.stop = threading.Event()
        node = cli.session().node

        def run():
            while not self.stop.wait(0.05):
                node.put(key, payload, **options)

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()


# --- topic list, schema, rpc, rehydrate, meta ------------------------------------------------


def test_topic_list(rover):
    beacon = Beacon("beacon", synq_authoritive=True)
    code, out, err = invoke("topic", "list")
    found = {d["topic"]: d for d in documents(out)}
    assert found["host/rover"]["type"] == "urn:pypi:spiri-rover:1.0:spirirover.Rover"
    assert set(found["host/rover"]) == {"topic", "owner", "type", "bases"}
    assert "object(s)" in err
    code, out, _ = invoke("topic", "list", "--type", "Rover", "--prefix", "./")
    assert [d["topic"] for d in documents(out)] == ["host/rover"]
    _, out, err = invoke("topic", "list", "--type", "NoSuchType", "--timeout", "0.3")
    assert documents(out) == [] and "0 object(s)" in err
    _ = beacon


def test_a_bad_topic_is_an_error():
    code, _, err = invoke("topic", "list", "--prefix", "a/../b")
    assert code == 1 and "Invalid topic" in err


def test_topic_schema(rover):
    code, out, _ = invoke("topic", "schema", "./rover")
    schema = yaml.safe_load(out)
    assert schema["$id"] == "urn:pypi:spiri-rover:1.0:spirirover.Rover"
    assert "drive" in schema["x-remote-methods"]
    code, out, err = invoke("topic", "schema", "host/nothing")
    assert code == 0 and out == "" and "No schema" in err


def test_topic_rpc(rover):
    beacon = Beacon("beacon", synq_authoritive=True)
    _, out, err = invoke("topic", "rpc", "./rover")
    (found,) = documents(out)
    assert found["topic"] == "host/rover"
    assert set(found["methods"]) == {"drive", "countdown", "snapshot"}
    assert "1 object(s)" in err
    # Every object: the beacon has no methods.
    wait_for(lambda: len(list(cli.session().list_topics(prefix="host", timeout=0.3))) == 2)
    _, out, _ = invoke("topic", "rpc", "--prefix", "host")
    assert [d["topic"] for d in documents(out)] == ["host/rover"]
    _, out, err = invoke("topic", "rpc", "host/nothing")
    assert out == "" and "nothing serves host/nothing" in err
    _ = beacon


def test_topic_rehydrate(rover):
    rover.speed = 2.5
    rover.picture = b"\x00\xff"
    _, out, _ = invoke("topic", "rehydrate", "./rover")
    state = yaml.safe_load(out)
    assert state == {"speed": 2.5, "name": "rover", "picture": b"\x00\xff"}
    _, out, err = invoke("topic", "rehydrate", "host/nothing")
    assert out == "" and "No state" in err


def test_meta_type_schema(rover):
    _, out, _ = invoke("meta", "type_schema", "Rover")
    (found,) = documents(out)
    assert found["topic"] == "host/rover" and found["schema"]["title"] == "Rover"
    _, out, err = invoke("meta", "type_schema", "Nothing", "--prefix", "./")
    assert out == "" and "No objects of type 'Nothing'" in err


# --- topic call ---------------------------------------------------------------------------


def test_topic_call(rover):
    code, out, err = invoke("topic", "call", "./rover/drive", "distance=2", "fast=true")
    assert code == 0 and documents(out) == [4.0] and "done" in err
    _, out, _ = invoke("topic", "call", "host/rover/countdown", "start=3")
    assert documents(out) == [3, 2, 1]
    # A value that isn't YAML is its text: here, the wrong type.
    code, _, err = invoke("topic", "call", "./rover/drive", "distance=[")
    assert code == 1 and "distance" in err
    code, _, err = invoke("topic", "call", "./rover/drive", "distance=-1")
    assert code == 1 and "ValueError: can't drive backwards" in err


def test_a_raw_reply_prints_as_binary(rover):
    _, out, _ = invoke("topic", "call", "./rover/snapshot")
    assert documents(out) == [b"\x89PNG"]


def test_topic_call_errors(rover):
    code, _, err = invoke("topic", "call", "./rover/drive", "badarg")
    assert code == 1 and "isn't key=value" in err
    code, _, err = invoke("topic", "call", "host/nobody/drive", "--timeout", "0.3")
    assert code == 2 and "no reply" in err


def test_a_call_cut_short(rover):
    """Replies that stop without the end marker: zenoh's timeout cut the call short."""
    node = cli.session().node
    owner = _core.Node(config(node.locators()[0]))
    try:
        # An object whose method answers once, then never ends the call.
        obj = owner.create_owner("host/slow", _core.Schema(BATTERY))
        wait_for(lambda: any(r.ok for r in node.get("host/slow/sr_metadata", timeout=0.3)))

        def answer():
            call = None
            while call is None or not isinstance(call, _core.Call):
                call = owner.next_notice(5)
            owner.reply(call.query, 7)

        thread = threading.Thread(target=answer)
        thread.start()
        code, out, err = invoke("topic", "call", "host/slow/set_limit", "percent=7", "-t", "1")
        thread.join()
        assert documents(out) == [7]
        assert code == 2 and "cut short" in err
        _ = obj
    finally:
        owner.close()


# --- topic watch, bandwidth, put ------------------------------------------------------------


def test_topic_watch():
    with Putter("host/w/a", b"{x: 1}"):
        code, out, err = invoke("topic", "watch", "./w/a", "-n", "1", "-RT")
    assert out.strip() == "{x: 1}" and "Watching" in err


def test_topic_watch_with_metadata():
    with Putter("host/w/a", b"line one\nline two"):
        _, out, _ = invoke("topic", "watch", "./w/*", "-n", "2", "--timestamp")
    (first, second) = documents(out)
    assert first["path"] == "host/w/a" and first["value"] == "line one\nline two"
    assert first["timestamp"] is None and "received" in first
    assert second["path"] == "host/w/a"


def test_topic_watch_json_and_binary():
    with Putter("host/w/b", b"\x00\xff", encoding="zenoh/bytes"):
        _, out, _ = invoke("topic", "watch", "host/w/b", "-n", "1", "--json", "-p")
    record = json.loads(out)
    assert record["path"] == "host/w/b" and record["value"] == "!!binary AP8="
    with Putter("host/w/c", b"\xff", encoding="text/plain"):
        _, out, _ = invoke("topic", "watch", "host/w/c", "-n", "1", "-j", "-RT")
    assert json.loads(out) == "!!binary /w=="
    with Putter("host/w/c", b"plain"):
        _, out, _ = invoke("topic", "watch", "host/w/c", "-n", "1", "-p", "-t")
    (record,) = documents(out)
    assert record["value"] == "plain" and record["timestamp"] is None


def test_topic_watch_stops_on_ctrl_c():
    timer = threading.Timer(0.3, _thread.interrupt_main)
    timer.start()
    code, _, err = invoke("topic", "watch", "host/quiet")
    assert code == 0 and "Stopped." in err


def test_topic_bandwidth():
    with Putter("host/bw", b"12345"):
        code, out, err = invoke("topic", "bandwidth", "./bw", "-i", "0.3", "-n", "2", "--bytes")
    assert all(int(n) % 5 == 0 for n in out.split())
    assert sum(int(n) for n in out.split()) > 0
    with Putter("host/bw", b"12345"):
        _, out, err = invoke("topic", "bandwidth", ".", "-i", "0.2", "-n", "1")
    assert "B/s" in err and "msg/s" in err


def test_topic_bandwidth_stops_on_ctrl_c():
    timer = threading.Timer(0.3, _thread.interrupt_main)
    timer.start()
    _, _, err = invoke("topic", "bandwidth", "host/quiet", "-i", "5")
    assert "Stopped." in err


def test_rates():
    assert cli.rate(10) == "10.0 B/s"
    assert cli.rate(2048) == "2.0 KB/s"
    assert cli.rate(1024**4 * 3) == "3.0 TB/s"


def test_topic_put(rover):
    mirror = Rover.from_topic("host/rover")
    invoke("topic", "put", "./rover/speed", "3.5")
    wait_for(lambda: mirror.speed == 3.5)
    code, _, err = invoke("topic", "put", "./rover/name", "-i", "raw", input="not: yaml\n")
    wait_for(lambda: mirror.name == "not: yaml")
    assert "Putting host/rover/name" in err
    invoke("topic", "put", "./rover/name", "[unclosed")  # not YAML: its text
    wait_for(lambda: mirror.name == "[unclosed")
    invoke("topic", "put", "./rover/name", "-i", "raw", "12")  # text, though it reads as YAML
    wait_for(lambda: mirror.name == "12")


def test_topic_put_a_stream(rover):
    mirror = Rover.from_topic("host/rover")
    stream = "path: host/rover/speed\nvalue: 4.5\n---\npath: ./rover/name\nvalue: piped\n---\n"
    invoke("topic", "put", "./rover", input=stream)
    wait_for(lambda: (mirror.speed, mirror.name) == (4.5, "piped"))
    invoke("topic", "put", input="path: host/rover/speed\nvalue: 5.5\n")
    wait_for(lambda: mirror.speed == 5.5)
    invoke("topic", "put", "./rover/speed", input="6.5\n---\n\n")
    wait_for(lambda: mirror.speed == 6.5)
    _, _, err = invoke("topic", "put", input="7.5\n")
    assert "No path and no topic" in err


def test_topic_put_errors(monkeypatch):
    code, _, err = invoke("topic", "put", "-i", "raw", input="value\n")
    assert code == 1 and "needs a topic" in err
    code, _, err = invoke("topic", "put", "./rover", input="path: other/x\nvalue: 1\n")
    assert code == 1 and "other/x isn't under host/rover" in err
    code, _, err = invoke("topic", "put", "./x", "-i", "yaml", input="[unclosed\n")
    assert code == 1 and "not YAML" in err
    monkeypatch.setattr(cli, "interactive", lambda: True)
    code, _, err = invoke("topic", "put", "./x")
    assert code == 1 and "stdin is a terminal" in err


# --- zenoh --------------------------------------------------------------------------------


def test_zenoh_info():
    _, out, _ = invoke("zenoh", "info")
    info = yaml.safe_load(out)
    assert info["zid"] == cli.session().zid
    assert info["routers"] == [] and info["peers"] and info["locators"]


def hello(zid: bytes, locators: list[str]) -> bytes:
    """A zenoh Hello from a peer: see tests/core/test_raw.py."""
    out = bytes([0x22, 0x09, 0x01 | ((len(zid) - 1) << 4)]) + zid + bytes([len(locators)])
    for locator in locators:
        out += bytes([len(locator)]) + locator.encode()
    return out


def test_zenoh_scout():
    fake = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fake.bind(("127.0.0.1", 0))
    fake.settimeout(5)

    def answer():
        _, sender = fake.recvfrom(1024)
        fake.sendto(hello(bytes(range(1, 17)), ["tcp/127.0.0.1:7447"]), sender)

    thread = threading.Thread(target=answer)
    thread.start()
    config = {
        "scouting": {
            "multicast": {"address": f"127.0.0.1:{fake.getsockname()[1]}", "interface": "127.0.0.1"}
        }
    }
    code, out, err = invoke("zenoh", "scout", "-t", "1", "--clients", "-c", json.dumps(config))
    thread.join()
    fake.close()
    (found,) = documents(out)
    assert found["whatami"] == "peer" and found["locators"] == ["tcp/127.0.0.1:7447"]
    assert "1 node(s) found" in err


def test_zenoh_scout_needs_something_to_look_for():
    code, _, err = invoke("zenoh", "scout", "--no-routers", "--no-peers")
    assert code == 1 and "at least one" in err


# --- the rest -----------------------------------------------------------------------------


def test_truncation():
    code, _, _ = invoke("--truncate", "zenoh", "info", truncate=True)
    assert code == 0
    code, _, _ = invoke("--no-truncate", "zenoh", "info")
    assert code == 0


def test_the_cli_opens_no_session_to_print_a_script(monkeypatch):
    monkeypatch.setattr(cli, "session", lambda: pytest.fail("opened a session"))
    result = runner.invoke(cli.app, ["completion", "bash"])
    assert result.exit_code == 0 and "complete -F" in result.output


def test_complete_server_command(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "SpiriSynq.completion.server.serve",
        lambda connect, idle: called.update(connect=connect, idle=idle),
    )
    invoke("_complete-server", "--idle", "1", "--connect", "tcp/127.0.0.1:1")
    assert called == {"connect": ["tcp/127.0.0.1:1"], "idle": 1.0}


def test_payload_values():
    def message(payload: bytes, encoding: str):
        return SimpleNamespace(payload=payload, encoding=encoding)

    assert cli.payload_value(message(b"{a: 1}", "application/yaml")) == {"a": 1}
    assert cli.payload_value(message(b"[", "application/yaml")) == b"["
    assert cli.payload_value(message(b"\x89PNG", "image/png")) == b"\x89PNG"
