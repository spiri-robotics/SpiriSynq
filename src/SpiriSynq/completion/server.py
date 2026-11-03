"""The tab-completion server, ``synq _complete-server``: one warm process that answers Tabs.

It holds a zenoh session and keeps the list of objects current by asking
``**/sr_metadata/*`` every ``SCAN_EVERY`` seconds; the per-tag keys give
each object's topic and tags without parsing a payload. It writes them to
the cache for the fallback. An object's schema (its RPC methods and their
parameters) is fetched when a Tab needs it and kept ``SCHEMA_TTL`` seconds.
A Tab gets an answer within ``DEADLINE`` seconds: a schema that isn't back
by then is left to arrive for the next Tab. It only ever reads metadata and
schemas, never calls a method.

Requests are one line, tab-separated: protocol, token, shell, the
``$SPIRI_SYNQ_BASE_TOPIC`` of the shell, bash's current word, and the line
up to the cursor. The reply is what completion.answer returns. Over the
Unix socket, which only its owner can open, the token is ignored; over TCP
it must match the one in ``complete.tcp``. Requests are answered one at a
time.

One server runs per runtime directory (a lock on ``complete.lock``, which
holds its pid while it runs), and it exits after ``idle`` seconds without a
request, on SIGTERM, or after a request once its own code has changed, so
the next Tab starts one that's up to date.
"""
import fcntl
import hmac
import json
import os
import secrets
import select
import signal
import socket
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

import yaml
import zenoh
from loguru import logger

from SpiriSynq import completion

IDLE = 600.0
DEADLINE = 0.3
SCAN_EVERY = 5.0
FIRST_SCAN = 1.5
"""Seconds the first scan listens: a new session needs a moment to learn what's out there."""
SCHEMA_TTL = 5.0
QUERY_TIMEOUT = 5.0

METADATA = "/sr_metadata/"


def scan(z: zenoh.Session, window: float) -> dict[str, list[str]]:
    """Every object that answers within ``window`` seconds, with its tags, asking again as the session learns routes."""
    found: defaultdict[str, set[str]] = defaultdict(set)
    deadline = time.monotonic() + window
    while (remaining := deadline - time.monotonic()) > 0:
        for reply in z.get(f"**{METADATA}*", consolidation=zenoh.ConsolidationMode.NONE, timeout=remaining):
            if reply.ok:
                topic, _, tag = str(reply.ok.key_expr).rpartition(METADATA)
                found[topic].add(tag)
        time.sleep(0.1)
    return {topic: sorted(tags) for topic, tags in found.items()}


def fetch_schema(z: zenoh.Session, topic: str) -> dict | None:
    for reply in z.get(f"{topic}/sr_object_schema", timeout=QUERY_TIMEOUT):
        if reply.ok:
            schema = yaml.safe_load(reply.ok.payload.to_string())    # plain JSON values, by the spec
            return schema if isinstance(schema, dict) else None
    return None


class LiveSource:
    """What the completion callbacks complete from, inside the server."""

    def __init__(self, z: zenoh.Session, cache: Path, base_topic: str):
        self.z = z
        self.cache = cache
        self.default_base = base_topic
        self.base: str | None = None
        self.deadline = 0.0
        self._topics = completion.read_topics(cache)
        self._schemas: dict[str, tuple[float, object]] = {}
        self._pool = ThreadPoolExecutor(4, thread_name_prefix="schema")
        self._stop = threading.Event()
        self._scanner = threading.Thread(target=self._scan_loop, name="scan", daemon=True)
        self._scanner.start()

    def topics(self) -> dict[str, list[str]]:
        return self._topics

    def base_topic(self) -> str:
        return self.base or self.default_base

    def schema(self, topic: str) -> dict | None:
        """``topic``'s object schema, or none if it isn't back by the request's deadline."""
        now = time.monotonic()
        cached = self._schemas.get(topic)
        if cached is None or cached[0] < now:
            cached = (now + SCHEMA_TTL, self._pool.submit(fetch_schema, self.z, topic))
            self._schemas[topic] = cached
        try:
            return cached[1].result(timeout=max(0.0, self.deadline - time.monotonic()))
        except FutureTimeout:
            return None
        except Exception as e:      # no object there, a bad reply: nothing to offer
            logger.debug(f"schema of {topic}: {e!r}")
            return None

    def _scan_loop(self) -> None:
        window = FIRST_SCAN
        while not self._stop.is_set():
            try:
                found = scan(self.z, window)
            except Exception as e:
                logger.warning(f"scanning for objects: {e!r}")
            else:
                if found != self._topics or not self.cache.exists():
                    self._topics = found
                    completion.write_topics(self.cache, found)
            window = 0.5
            self._stop.wait(SCAN_EVERY)

    def close(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)


def mtimes(paths) -> list[float | None]:
    out = []
    for path in paths:
        try:
            out.append(path.stat().st_mtime)
        except FileNotFoundError:
            out.append(None)
    return out


def serve(connect: list[str] = (), idle: float = IDLE, run: Path | None = None) -> None:
    """Answer Tabs until idle. Returns at once if another server holds this runtime directory.

    ``connect``: zenoh endpoints to connect to, with multicast scouting off.
    Without any, the session is the default one, as ``synq`` itself uses.
    """
    run = run or completion.runtime_dir()
    run.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = open(run / "complete.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    lock.truncate(0)
    lock.write(f"{os.getpid()}\n")
    lock.flush()

    import typer
    from SpiriSynq import cli
    from SpiriSynq.session import Session, _default_base_topic, current_session

    # The base topic comes with each request; this one is for shells that don't set it.
    os.environ.pop("SPIRI_SYNQ_BASE_TOPIC", None)
    if connect:
        config = zenoh.Config()
        config.insert_json5("connect/endpoints", json.dumps(list(connect)))
        config.insert_json5("scouting/multicast/enabled", "false")
        session = Session(config=config)
    else:
        session = current_session.get()
    watch = [Path(completion.__file__), Path(__file__), Path(cli.__file__)]
    before = mtimes(watch)
    live = completion.source = LiveSource(session.zenoh_session, completion.cache_path(), _default_base_topic())
    command = typer.main.get_command(cli.app)
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    sock_path, tcp_path = run / "complete.sock", run / "complete.tcp"
    listeners = []
    unix = socket.socket(socket.AF_UNIX)
    try:
        sock_path.unlink(missing_ok=True)
        unix.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        unix.listen()
        listeners.append(unix)
    except OSError as e:            # a path too long for a Unix socket: bash's TCP still works
        logger.warning(f"no Unix socket at {sock_path}: {e}")
        unix.close()
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen()
    listeners.append(tcp)
    token = secrets.token_hex(16)
    tmp = run / f".complete.tcp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{tcp.getsockname()[1]} {token}\n")
    os.replace(tmp, tcp_path)

    try:
        last = time.monotonic()
        while (left := last + idle - time.monotonic()) > 0:
            ready, _, _ = select.select(listeners, [], [], left)
            for listener in ready:
                conn, _ = listener.accept()
                with conn:
                    handle(conn, command, live, token if listener is tcp else None)
                last = time.monotonic()
            if ready and mtimes(watch) != before:
                logger.info("code changed; exiting")
                break
    finally:
        for listener in listeners:
            listener.close()
        if unix in listeners:
            sock_path.unlink(missing_ok=True)
        tcp_path.unlink(missing_ok=True)
        lock.truncate(0)            # a pid in the lock file is a running server
        live.close()
        session.close()


def handle(conn: socket.socket, command, live: LiveSource, token: str | None) -> None:
    """Answer one request. A bad one is dropped, so the shell gets nothing."""
    conn.settimeout(1.0)
    data = b""
    try:
        while not data.endswith(b"\n") and len(data) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        protocol, their_token, shell, base, cur, line = data.decode().rstrip("\n").split("\t", 5)
    except (OSError, UnicodeDecodeError, ValueError):
        return
    if protocol != completion.PROTOCOL or (token is not None and not hmac.compare_digest(their_token, token)):
        return
    live.base = base or None
    live.deadline = time.monotonic() + DEADLINE
    try:
        reply = completion.answer(command, shell, line, cur)
    except Exception as e:
        logger.exception(f"completing {line!r}: {e!r}")
        return
    try:
        conn.sendall((reply + "\n").encode() if reply else b"")
    except OSError:
        pass
