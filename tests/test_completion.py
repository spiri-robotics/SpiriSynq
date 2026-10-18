"""Tab completion: the callbacks, the shell classes, the server, and real shells typing Tab."""
import fcntl
import os
import pty
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

import pytest
import typer

import conftest
from SpiriSynq import completion
from SpiriSynq.cli import app
from SpiriSynq.completion.server import LiveSource
from SpiriSynq.remote_callables import remote_method
from SpiriSynq.schema import get_schema
from SpiriSynq.syncable_objects import SubSyncableDataclass, SyncableObject

CLI = typer.main.get_command(app)
ROOT = Path(__file__).parent.parent


class Gait(Enum):
    walk = "walk"
    trot = "trot"


@dataclass
class Leg(SubSyncableDataclass):
    length: float = 1.0


@dataclass
class CompletionRobot(SyncableObject):
    speed: float = 0.0

    @remote_method()
    def move(self, gait: Gait, gear: Literal[1, 2, "r"], fly: bool = False,
             height: float | None = 1.5, leg: Leg | None = None) -> None:
        """Move the robot.

        Gently."""

    @remote_method()
    def ping(self) -> str:
        return "pong"


TOPICS = {"host/robot": ["CompletionRobot", "SyncableObject"], "host/robot/arm": ["Arm"],
          "other/thing": ["Thing"]}


class FakeSource:
    def __init__(self, base="host"):
        self.base = base
        self.asked = []

    def topics(self):
        return TOPICS

    def base_topic(self):
        return self.base

    def schema(self, topic):
        self.asked.append(topic)
        return get_schema(CompletionRobot) if topic == "host/robot" else None


@pytest.fixture(autouse=True)
def started(monkeypatch):
    """In this process, the fallback never starts a real server; it's recorded instead."""
    calls = []
    monkeypatch.setattr(completion, "start_server", lambda *a: calls.append(a))
    return calls


@pytest.fixture
def fake(monkeypatch):
    source = FakeSource()
    monkeypatch.setattr(completion, "source", source)
    return source


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """The topics cache the fallback reads, under a private XDG_CACHE_HOME."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SPIRI_SYNQ_BASE_TOPIC", "host")
    path = completion.cache_path()
    completion.write_topics(path, TOPICS)
    return path


@pytest.fixture
def short_dir():
    """A directory with a path short enough for a Unix socket (107 bytes), which tmp_path may not be."""
    path = Path(tempfile.mkdtemp(prefix="synq", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def values(found):
    return [v[0] if isinstance(v, tuple) else v for v in found]


# ── Callbacks ───────────────────────────────────────────────────────────

def test_cache_round_trips(cache):
    assert completion.read_topics(cache) == TOPICS


def test_topic_completes_one_chunk_at_a_time(fake):
    assert completion.complete_topic("") == ["./", "host/", "other/"]
    assert completion.complete_topic("host/") == ["host/robot", "host/robot/"]
    assert completion.complete_topic("host/robot/") == ["host/robot/arm"]
    assert completion.complete_topic("nowhere") == []


def test_topic_completes_relative_to_the_base_topic(fake):
    assert completion.complete_topic(".") == ["./"]
    assert completion.complete_topic("./r") == ["./robot", "./robot/"]
    fake.base = "elsewhere"
    assert completion.complete_topic("") == ["host/", "other/"]          # nothing under it: no ./


def test_tag_completes_without_the_bang(fake):
    assert completion.complete_tag("") == ["Arm", "CompletionRobot", "SyncableObject", "Thing"]
    assert completion.complete_tag("S") == ["SyncableObject"]


def test_method_completes_below_an_objects_topic(fake):
    assert values(completion.complete_method("host/")) == ["host/robot/"]
    assert completion.complete_method("host/robot/") == [
        "host/robot/arm/", ("host/robot/move", "Move the robot."), ("host/robot/ping", "")]
    assert values(completion.complete_method("./robot/m")) == ["./robot/move"]


def test_method_hides_the_builtins_unless_asked(fake):
    assert values(completion.complete_method("host/robot/sr_")) == [
        "host/robot/sr_metadata", "host/robot/sr_object_schema", "host/robot/sr_rehydrate"]


def arguments(line: str) -> list:
    """What complete_arguments offers for the last word of ``synq topic call ...``."""
    *words, incomplete = line.split(" ")
    ctx = CLI.make_context("synq", ["topic", "call", *words], resilient_parsing=True)
    call = ctx.command.get_command(ctx, "topic").get_command(ctx, "call")
    sub = call.make_context("call", words, parent=ctx, resilient_parsing=True)
    return completion.complete_arguments(sub, incomplete)


def test_arguments_offer_each_parameter_with_its_type(fake):
    assert arguments("host/robot/move ") == [
        ("gait=", "Gait (required)"), ("gear=", "1 | 2 | r (required)"), ("fly=", "boolean"),
        ("height=", "number | null"), ("leg=", "Leg | null")]
    assert values(arguments("host/robot/move gait=walk fly=true ")) == ["gear=", "height=", "leg="]
    assert values(arguments("host/robot/move h")) == ["height="]


def test_argument_values_follow_the_type_hints(fake):
    assert arguments("host/robot/move gait=") == ["gait=walk", "gait=trot"]
    assert arguments("host/robot/move gear=") == ["gear=1", "gear=2", "gear=r"]
    assert arguments("host/robot/move fly=") == ["fly=true", "fly=false"]
    assert arguments("host/robot/move height=") == ["height=null", "height=1.5"]
    assert arguments("host/robot/move leg=") == ["leg=null"]
    assert arguments("host/robot/move gait=t") == ["gait=trot"]
    assert arguments("./robot/move gait=w") == ["gait=walk"]


def test_arguments_for_an_unknown_method_or_parameter(fake):
    assert arguments("host/robot/nothing ") == []
    assert arguments("other/thing/move ") == []
    assert arguments("host/robot/move nothing=") == []


def test_fallback_offers_no_methods(cache):
    assert completion.complete_method("host/robot/") == ["host/robot/arm/"]
    assert completion.complete_topic("./r") == ["./robot", "./robot/"]


# ── Shell classes ───────────────────────────────────────────────────────

def bash(line: str, cur: str) -> list[str]:
    """What the bash class answers for ``line``, whose last word bash split as ``cur``."""
    return completion.answer(CLI, "bash", line, cur).splitlines()


def zsh(line: str) -> str:
    return completion.answer(CLI, "zsh", line)


def test_bash_completes_commands_topics_and_tags(fake, started):
    assert bash("synq to", "to") == ["topic"]
    assert bash("synq topic watch ho", "ho") == ["host/"]
    assert bash("synq topic list --type C", "C") == ["CompletionRobot"]
    assert bash("synq topic call host/robot/m", "host/robot/m") == ["host/robot/move"]
    assert not started                  # the server itself answers these


def test_bash_answers_relative_to_its_word_after_an_equals(fake):
    # bash splits "gait=w" into "gait" "=" "w", and replaces only the "w".
    assert bash("synq topic call host/robot/move gait=w", "w") == ["walk"]
    assert bash("synq topic call host/robot/move gait=", "=") == ["=walk", "=trot"]
    assert bash("synq topic call host/robot/move ga", "ga") == ["gait="]


def test_bash_fallback_reads_the_environment_and_starts_the_server(cache, monkeypatch, started):
    monkeypatch.setenv("COMP_LINE", "synq topic watch ho")
    monkeypatch.setenv("COMP_CUR", "ho")
    assert completion.BashComplete(CLI, {}, "synq", "_SYNQ_COMPLETE").complete() == "host/"
    assert len(started) == 1


def test_bash_still_answers_typers_own_script(cache, monkeypatch):
    monkeypatch.setenv("COMP_WORDS", "synq topic watch ho")
    monkeypatch.setenv("COMP_CWORD", "3")
    assert completion.BashComplete(CLI, {}, "synq", "_SYNQ_COMPLETE").complete() == "host/"


def test_zsh_adds_no_space_after_a_slash_or_an_equals(fake):
    assert zsh("synq topic watch ho") == "compadd -p '' -S '' -- host/"
    assert zsh("synq topic watch host/") == "compadd -p host/ -S '' -- robot/; compadd -p host/ -- robot"
    assert zsh("synq topic call host/robot/move fl") == \
        "local -a described=(fly=:boolean); _describe -t values '' described -p '' -S ''"
    assert zsh("synq topic call host/robot/move gait=").startswith("_arguments")


def test_zsh_shows_method_descriptions(fake):
    assert zsh("synq topic call host/robot/") == (
        "compadd -p host/robot/ -S '' -- arm/; "
        "local -a described=('move:Move the robot.' ping); _describe -t values '' described -p host/robot/")


def test_zsh_offers_no_local_files(fake):
    assert zsh("synq topic watch nowhere") == ""
    assert zsh("synq topic put host/robot nothing") == ""


def test_completion_prints_each_script():
    for shell in completion.Shell:
        assert "_SYNQ_COMPLETE" in completion.script(shell)
    for shell in ("bash", "zsh"):
        assert "spirisynq" in completion.script(shell)


def test_completion_command_prints_the_script():
    from typer.testing import CliRunner
    result = CliRunner().invoke(app, ["completion", "zsh"])
    assert result.exit_code == 0
    assert result.output.startswith("#compdef synq spirisynq\n")


# ── Starting the server ─────────────────────────────────────────────────

def test_start_server_starts_one(short_dir, monkeypatch):
    monkeypatch.undo()                  # the real start_server
    spawned = []
    monkeypatch.setattr(completion.subprocess, "Popen", lambda argv, **kwargs: spawned.append(argv))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))
    completion.start_server()
    completion.start_server()
    assert spawned == [[sys.executable, "-m", "SpiriSynq", "_complete-server"]]
    assert (short_dir / "spirisynq").stat().st_mode & 0o777 == 0o700


def test_start_server_leaves_a_running_one_alone(short_dir, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(completion.subprocess, "Popen", lambda argv, **kwargs: pytest.fail("started another"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))
    run = short_dir / "spirisynq"
    run.mkdir()
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(run / "complete.sock"))
        s.listen()
        completion.start_server()


# ── The server's source, against a real object ──────────────────────────

def eventually(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not (result := fn()) and time.monotonic() < deadline:
        time.sleep(0.05)
    return result


@pytest.fixture
def robot(zenoh_current_session):
    return CompletionRobot("completion/robot", synq_authoritive=True)


def test_live_source_finds_objects_and_schemas(robot, zenoh_current_session, tmp_path):
    topic = robot.synq_absolute_path
    live = LiveSource(zenoh_current_session.zenoh_session, tmp_path / "topics", "host")
    try:
        assert eventually(lambda: topic in live.topics())
        assert live.topics()[topic] == ["CompletionRobot", "SyncableObject"]
        assert completion.read_topics(tmp_path / "topics")[topic] == ["CompletionRobot", "SyncableObject"]
        live.deadline = time.monotonic() + 10
        assert set(live.schema(topic)["x-rpc-endpoints"]) >= {"move", "ping"}
        assert live.schema("no/such/object") is None
    finally:
        live.close()


def test_live_source_answers_by_the_deadline(robot, zenoh_current_session, tmp_path):
    topic = robot.synq_absolute_path
    live = LiveSource(zenoh_current_session.zenoh_session, tmp_path / "topics", "host")
    try:
        live.deadline = time.monotonic()      # no time at all: nothing yet, but it's on its way
        assert live.schema(topic) is None
        live.deadline = time.monotonic() + 10
        assert "move" in live.schema(topic)["x-rpc-endpoints"]
    finally:
        live.close()


# ── The server process ──────────────────────────────────────────────────

class Server:
    def __init__(self, env: dict, run: Path, process: subprocess.Popen):
        self.env, self.run, self.process = env, run, process

    def request(self, shell: str, line: str, cur: str = "", base: str = "", *, tcp: bool = False,
                token: str | None = None, protocol: str = completion.PROTOCOL) -> str:
        if tcp:
            port, real = (self.run / "complete.tcp").read_text().split()
            conn = socket.create_connection(("127.0.0.1", int(port)))
            token = real if token is None else token
        else:
            conn = socket.socket(socket.AF_UNIX)
            conn.connect(str(self.run / "complete.sock"))
        with conn:
            conn.sendall(f"{protocol}\t{token or ''}\t{shell}\t{base}\t{cur}\t{line}\n".encode())
            conn.settimeout(5)
            data = b""
            while chunk := conn.recv(4096):
                data += chunk
        return data.decode()


def start(env: dict, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "SpiriSynq", "_complete-server",
         "--connect", f"tcp/127.0.0.1:{conftest._seed_port}", *args],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def server_env(tmp_path, short_dir):
    env = {**os.environ, "XDG_RUNTIME_DIR": str(short_dir), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "PYTHONPATH": str(ROOT)}
    env.pop("SPIRI_SYNQ_BASE_TOPIC", None)
    return env


@pytest.fixture
def server(server_env, short_dir):
    process = start(server_env, "--idle", "60")
    run = short_dir / "spirisynq"
    assert eventually(lambda: (run / "complete.tcp").exists()), "the server didn't start"
    yield Server(server_env, run, process)
    process.terminate()
    process.wait(timeout=10)


def test_server_answers_both_sockets(server, robot):
    topic = robot.synq_absolute_path
    assert server.request("zsh", "synq to").startswith("_arguments '*: :((\"topic\"")
    assert eventually(lambda: server.request("bash", f"synq topic schema {topic}", topic, tcp=True)
                      == f"{topic}\n")
    line = f"synq topic call {topic}/move gait="
    assert eventually(lambda: server.request("bash", line, "=", tcp=True)) == "=walk\n=trot\n"
    assert server.request("zsh", f"synq topic call {topic}/move fl") == \
        "local -a described=(fly=:boolean); _describe -t values '' described -p '' -S ''\n"


def test_server_takes_the_base_topic_from_the_request(server, robot):
    topic = robot.synq_absolute_path
    base, _, rest = topic.partition("/")
    assert eventually(lambda: server.request("bash", "synq topic schema ./", "./", base=base, tcp=True))
    assert server.request("bash", f"synq topic schema ./{rest}", f"./{rest}", base=base, tcp=True) == \
        f"./{rest}\n"
    assert server.request("bash", f"synq topic schema ./{rest}", f"./{rest}", base="nowhere", tcp=True) == ""


def test_server_drops_a_bad_request(server):
    assert server.request("bash", "synq to", "to", tcp=True) == "topic\n"
    assert server.request("bash", "synq to", "to", tcp=True, token="wrong") == ""
    assert server.request("bash", "synq to", "to", protocol="0") == ""
    assert server.request("fish", "synq to") == ""
    assert server.request("bash", "synq to", "to", tcp=True) == "topic\n"   # still up


def test_one_server_per_runtime_dir(server):
    assert (server.run / "complete.lock").read_text() == f"{server.process.pid}\n"
    second = start(server.env)
    assert second.wait(timeout=20) == 0
    assert server.process.poll() is None
    assert (server.run / "complete.lock").read_text() == f"{server.process.pid}\n"


def test_server_exits_when_idle_and_cleans_up(server_env, short_dir):
    process = start(server_env, "--idle", "1")
    assert process.wait(timeout=20) == 0
    assert sorted(p.name for p in (short_dir / "spirisynq").iterdir()) == ["complete.lock"]
    assert (short_dir / "spirisynq" / "complete.lock").read_text() == ""


# ── Real shells, typing Tab on a pty ────────────────────────────────────

def type_into(argv: list[str], env: dict, keys: str) -> str:
    """Start an interactive shell on a pty, type ``keys`` (pausing after each Tab), and return the screen."""
    fd, tty = pty.openpty()
    fcntl.ioctl(tty, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 500, 0, 0))   # wide: lines don't wrap
    shell = subprocess.Popen(argv, env=env, stdin=tty, stdout=tty, stderr=tty, start_new_session=True)
    os.close(tty)
    out = b""

    def pump(seconds):
        nonlocal out
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    out += os.read(fd, 4096)
                except OSError:
                    return

    pump(1.0)
    for chunk in re.split(r"(?<=\t)", keys):
        os.write(fd, chunk.encode())
        pump(1.5 if chunk.endswith("\t") else 0.2)
    os.write(fd, b"\x15exit\n")
    pump(0.3)
    shell.wait(timeout=5)
    os.close(fd)
    screen = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[=>]", "", out.decode(errors="replace"))
    while re.search(r"[^\x08]\x08", screen):                  # apply backspaces
        screen = re.sub(r"[^\x08]\x08", "", screen)
    return screen


def rc_head(prompt: str, bin_dir: Path) -> str:
    """The start of an rc file: ``bin_dir`` first on PATH (the system rc files run before it and may set PATH)."""
    return f"PS1='{prompt} '\nPATH={bin_dir}:$PATH\n"


def run_shell(shell: str, tmp_path: Path, env: dict, bin_dir: Path, keys: str) -> str:
    env = {**env, "HOME": str(tmp_path), "TERM": "xterm", "ZDOTDIR": str(tmp_path)}
    if shell == "bash":
        rc = tmp_path / "bashrc"
        rc.write_text(rc_head("$", bin_dir) + completion.script("bash"))
        return type_into(["bash", "--noprofile", "--rcfile", str(rc), "-i"], env, keys)
    fpath = tmp_path / "zfunc"
    fpath.mkdir()
    (fpath / "_synq").write_text(completion.script("zsh"))     # autoloaded, as installed
    (tmp_path / ".zshrc").write_text(rc_head("%", bin_dir) + f"fpath=({fpath} $fpath)\n"
                                     "autoload -U compinit; compinit -u -D\n")
    return type_into(["zsh", "-i"], env, keys)


def bin_with(tmp_path: Path, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "synq").write_text(f"#!/bin/sh\n{body}\n")
    (bin_dir / "synq").chmod(0o755)
    return bin_dir


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_shell_completes_rpc_arguments_from_the_server(shell, server, robot, tmp_path):
    if not shutil.which(shell):
        pytest.skip(f"no {shell}")
    topic = robot.synq_absolute_path
    assert eventually(lambda: server.request("bash", f"synq topic call {topic}", topic, tcp=True))   # scanned
    assert eventually(lambda: server.request("bash", f"synq topic call {topic}/move gait=", "=", tcp=True))
    no_python = bin_with(tmp_path, "exit 1")          # what completes must have come from the server
    screen = run_shell(shell, tmp_path, server.env, no_python, f"synq topic call {topic[:-2]}\tmo\tga\tw\t")
    assert f"synq topic call {topic}/move gait=walk " in screen


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_shell_falls_back_without_the_server(shell, tmp_path, cache):
    if not shutil.which(shell):
        pytest.skip(f"no {shell}")
    # Just started, as far as the fallback can tell: it won't start a server on the default session.
    (cache.parent / "complete.started").touch()
    env = {"PATH": os.environ["PATH"], "XDG_CACHE_HOME": str(cache.parent.parent), "PYTHONPATH": str(ROOT)}
    synq = bin_with(tmp_path, f'exec {sys.executable} -m SpiriSynq "$@"')
    screen = run_shell(shell, tmp_path, env, synq, "synq topic watch ho\tro\t")
    assert "synq topic watch host/robot" in screen
