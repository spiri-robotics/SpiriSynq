"""Sessions: the default session, discovery, base topics, and closing."""

import asyncio
import threading
from dataclasses import dataclass

import pytest

from SpiriSynq import session as session_module
from SpiriSynq.session import Session, _default_base_topic, _names, current_session
from SpiriSynq.syncable_objects import SyncableObject

from .conftest import config, wait_for


@dataclass
class Counter(SyncableObject):
    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000030:1"
    value: int = 0


def test_list_topics_finds_objects(session, other):
    first = Counter("counter", synq_authoritive=True)
    second = Counter("fleet/counter", synq_authoritive=True)
    found = {m["topic"]: m for m in other.list_topics(Counter)}
    assert set(found) >= {"host/counter", "host/fleet/counter"}
    meta = found["host/counter"]
    assert meta["owner"] == session.zid
    assert meta["type"] == "urn:uuid:1a2b3c4d-0000-4000-8000-000000000030:1"
    assert meta["classes"] == [meta["type"], *meta["bases"]]
    by_id = {m["topic"] for m in other.list_topics(meta["type"], prefix="host/fleet")}
    assert by_id == {"host/fleet/counter"}
    by_name = {m["topic"] for m in other.list_topics("Untyped")}
    assert "host/counter" not in by_name
    assert list(other.list_topics(timeout=0.01)) is not None
    first.close()
    second.close()


def test_type_names_match_the_end_of_type_ids():
    assert _names("urn:pypi:spiri:1.0:spiri.camera.Camera", "Camera")
    assert _names("urn:pypi:spiri:1.0:Camera", "Camera")
    assert not _names("urn:pypi:spiri:1.0:spiri.camera.Camera", "amera")
    assert not _names("urn:uuid:1a2b:1", "Camera")


def test_the_default_session_opens_on_first_use(monkeypatch, session):
    assert current_session.get() is session
    fresh = current_session.__class__()
    # The default session opens with zenoh's defaults; here, without multicast scouting.
    monkeypatch.setattr(session_module, "Session", lambda: Session(config()))
    monkeypatch.setenv("SPIRI_SYNQ_BASE_TOPIC", "elsewhere")
    made = fresh.get()
    try:
        assert made.base_topic == "elsewhere"
        assert fresh.get() is made
    finally:
        made.close()
    again = fresh.get()  # a closed default is replaced
    assert again is not made
    again.close()


def test_as_default(open_session):
    mine = open_session("mine")
    with mine.as_default() as s:
        assert s is mine
        assert Counter("c", synq_authoritive=True).synq_absolute_path == "mine/c"
    assert current_session.get() is not mine


def test_base_topic_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("SPIRI_SYNQ_BASE_TOPIC", "drone7")
    assert _default_base_topic() == "drone7"
    monkeypatch.delenv("SPIRI_SYNQ_BASE_TOPIC")
    assert _default_base_topic()


def test_closing_a_session(open_session):
    s = open_session("closing")
    counter = Counter("c", synq_authoritive=True, synq_session=s)
    s.close()
    s.close()  # twice is fine
    counter.close()
    request = object()
    with pytest.raises(RuntimeError, match="closed"):
        next(s.replies(request, 1.0))


def test_a_session_from_json_text():
    s = Session('{"scouting": {"multicast": {"enabled": false}}}', base_topic="t")
    assert s.zid
    s.close()
    plain = Session(config(), base_topic="t")
    plain.close()


def test_waiting_on_the_network_thread(session, other):
    counter = Counter("counter", synq_authoritive=True)
    got = []
    done = threading.Event()

    def on_value(value):
        # A blocking mirror inside a callback: the network thread keeps handling notices.
        got.append(Counter.from_topic("host/counter", session=session).value)
        done.set()

    counter.events.value.connect(on_value)
    mirror = Counter.from_topic("host/counter", session=other)
    mirror.value = 3
    wait_for(done.is_set)
    assert got == [3]
    assert not session.wait(threading.Event(), 0.01)


def test_replies_for_async_code(session):
    counter = Counter("counter", synq_authoritive=True)

    async def main():
        request = session.node.fetch_schema("host/counter")
        return [n async for n in session.areplies(request, 5)]

    ((value, error),) = asyncio.run(main())
    assert error is None and value["$id"].endswith(":1")
    counter.close()


def test_a_request_with_no_answer_times_out(session):
    request = session.node.discover("host/none")
    with pytest.raises(TimeoutError, match="no reply in time"):
        list(session.notices(request, 0.0))


def test_the_base_topic_file(monkeypatch):
    from SpiriSynq import session as module

    class FakePath:
        def __init__(self, path):
            self.path = path

        def read_text(self):
            return "machine\n"

    monkeypatch.delenv("SPIRI_SYNQ_BASE_TOPIC", raising=False)
    monkeypatch.setattr(module, "Path", FakePath)
    assert _default_base_topic() == "machine"


def test_a_session_uses_zenohs_defaults_without_a_config(monkeypatch):
    from SpiriSynq import session as module

    given = []

    class RefusedError(Exception):
        pass

    def node(config, **limits):
        given.append(config)
        raise RefusedError

    monkeypatch.setattr(module._core, "Node", node)
    with pytest.raises(RefusedError):
        Session(base_topic="t")
    assert given == ["{}"]


def test_a_schema_fetch_answered_with_an_error(session):
    class Node:
        def fetch_schema(self, topic):
            return "request"

    class Answer:
        error = ("PermissionError", "not you")
        value = None

    real = session.node
    session.node = Node()
    try:
        session._request_queue("request").put(Answer())
        with pytest.raises(LookupError, match="PermissionError: not you"):
            session.fetch_schema("host/x")
    finally:
        session.node = real


def test_waiting_on_the_network_thread_times_out_and_ends_with_the_session(open_session):
    s = open_session("waiting")
    counter = Counter("counter", synq_authoritive=True, synq_session=s)
    results = []
    started = threading.Event()

    def on_value(value):
        results.append(s.wait(threading.Event(), 0.15))  # times out, notices or not
        started.set()
        results.append(s.wait(threading.Event(), None))  # until the session closes

    counter.events.value.connect(on_value)
    mirror = Counter.from_topic("waiting/counter", session=s)
    mirror.value = 1
    wait_for(started.is_set)
    s.close()
    wait_for(lambda: len(results) == 2)
    assert results == [False, False]


def test_errors_in_handlers_are_logged(session, other):
    counter = Counter("counter", synq_authoritive=True)
    mirror = Counter.from_topic("host/counter", session=other)
    heard = []

    def broken(alive):
        heard.append(alive)
        raise RuntimeError("handler broke")

    mirror.synq_signal_alive.connect(broken)
    counter.close()
    wait_for(lambda: heard == [False])
    mirror.value = 1  # the network thread carries on


def test_sessions_close_at_exit(open_session):
    from SpiriSynq.session import _close_all

    s = open_session("exiting")
    _close_all()
    assert s._closed.is_set()
