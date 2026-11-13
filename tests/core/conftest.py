"""Fixtures for the tests of SpiriSynq._core: nodes on a loopback TCP network of their own."""

import json
import time

import pytest

from SpiriSynq import _core

BATTERY = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "x-spirisynq-schema": 3,
    "$id": "urn:pypi:spiri-robot:1.0:spirirobot.Battery",
    "x-bases": ["urn:pypi:spiri-robot:1.0:spirirobot.Part"],
    "properties": {
        "voltage": {"type": "number"},
        "anything": {},
        "image": {"type": "string", "x-skip-rehydrate": True},
    },
    "x-remote-methods": {
        "set_limit": {
            "parameters": {"type": "object", "properties": {"percent": {"type": "integer"}}},
            "returns": {"type": "integer"},
        },
        "read": {
            "x-key": "read/{path:**}",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            "returns": {"type": "string"},
        },
        "blob": {},
    },
}


def config(seed: str | None = None) -> str:
    """Loopback TCP only, no multicast scouting, connected to `seed` if given."""
    config = {
        "listen": {"endpoints": ["tcp/127.0.0.1:0"]},
        "scouting": {"multicast": {"enabled": False}},
    }
    if seed is not None:
        config["connect"] = {"endpoints": [seed]}
    return json.dumps(config)


def wait_until(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def wait_for(node: _core.Node, kind, check=lambda notice: True, timeout: float = 10.0):
    """The next notice of class `kind` that passes `check`, skipping the others."""
    deadline = time.monotonic() + timeout
    while True:
        notice = node.next_notice(max(0.0, deadline - time.monotonic()))
        assert notice is not None, f"no {kind.__name__} notice"
        if isinstance(notice, kind) and check(notice):
            return notice


@pytest.fixture
def network():
    """Opens `n` connected nodes, and closes them after the test."""
    opened: list[_core.Node] = []

    def open_network(n: int) -> list[_core.Node]:
        seed = _core.Node(config())
        nodes = [seed]
        for _ in range(1, n):
            nodes.append(_core.Node(config(seed.locators()[0])))
        opened.extend(nodes)
        for node in nodes:
            wait_until(lambda node=node: len(node.peers()) == n - 1)
        return nodes

    yield open_network
    for node in opened:
        node.close()


@pytest.fixture
def battery() -> _core.Schema:
    return _core.Schema(BATTERY)


def mirror(node: _core.Node, topic: str, schema: _core.Schema, **options) -> _core.ObjectId:
    """A mirror whose owner's state has arrived in full."""
    mirror = node.create_mirror(topic, schema, **options)
    wait_for(node, _core.Rehydrated, lambda n: n.object == mirror and n.complete)
    return mirror
