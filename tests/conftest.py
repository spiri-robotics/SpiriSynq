"""Test fixtures: sessions on a loopback TCP network of their own, with no multicast scouting.

One seed node listens on a random local port; every test session connects to it. Each test
gets a fresh default session (`session`) and can open more (`open_session`), all closed after
the test.
"""

import json
import time

import pytest

from SpiriSynq import _core
from SpiriSynq.session import Session, current_session

_seed: _core.Node | None = None


def config(connect: str | None = None) -> dict:
    config = {
        "listen": {"endpoints": ["tcp/127.0.0.1:0"]},
        "scouting": {"multicast": {"enabled": False}},
    }
    if connect is not None:
        config["connect"] = {"endpoints": [connect]}
    return config


def pytest_sessionstart(session):
    global _seed
    _seed = _core.Node(json.dumps(config()))


def pytest_sessionfinish(session, exitstatus):
    if _seed is not None:
        _seed.close()


def wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll `predicate` until it's true; fail the test if it isn't in time."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@pytest.fixture
def open_session():
    """Opens sessions connected to the test network, and closes them after the test."""
    opened: list[Session] = []

    def open_one(base_topic: str = "host", **options) -> Session:
        assert _seed is not None
        session = Session(config(_seed.locators()[0]), base_topic=base_topic, **options)
        # Connected to the seed and to every session the test opened before it.
        others = len(opened) + 1
        opened.append(session)
        wait_for(lambda: len(session.node.peers()) >= others)
        return session

    yield open_one
    for session in opened:
        session.close()


@pytest.fixture(autouse=True)
def session(request, open_session):
    """The default session for the test. The core's own tests (`tests/core`) don't get one."""
    if "core" in request.node.path.parts:
        yield None
        return
    s = open_session("host", call_timeout=5.0)
    token = current_session.set(s)
    yield s
    current_session.reset(token)


@pytest.fixture
def other(open_session):
    """A second session, as another process would have."""
    return open_session("other", call_timeout=5.0)
