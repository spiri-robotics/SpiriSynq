from conftest import zenoh_test_config
"""
Tests for relative topics: a leading ``.`` chunk expands to the session's base_topic,
so ``./camera`` and ``<base_topic>/camera`` name the same object.
"""

import time
from dataclasses import dataclass

import pytest

from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session


@pytest.fixture(autouse=True)
def close_test_sessions():
    from SpiriSynq.shutdown import _live_sessions
    before = set(_live_sessions.keys())
    yield
    for sid, session in list(_live_sessions.items()):
        if sid not in before:
            session.close()


def _wait_for(predicate, timeout=3.0, interval=0.01):
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def node_a():
    return Session(config=zenoh_test_config(), base_topic="rel_node_a")


@pytest.fixture
def node_b():
    return Session(config=zenoh_test_config(), base_topic="rel_node_b")


@dataclass
class RelCamera(SyncableObject):
    exposure: int = 0


# ── Session.resolve_topic ─────────────────────────────────────────────────────

@pytest.mark.parametrize("topic, expected", [
    ("./camera", "rel_node_a/camera"),
    ("./camera/sr_rehydrate", "rel_node_a/camera/sr_rehydrate"),
    (".", "rel_node_a"),
    ("./**", "rel_node_a/**"),
    ("rel_node_a/camera", "rel_node_a/camera"),
    ("other_node/camera", "other_node/camera"),
    ("**/sr_metadata", "**/sr_metadata"),
])
def test_resolve_topic(node_a, topic, expected):
    assert node_a.resolve_topic(topic) == expected


@pytest.mark.parametrize("topic", [
    "../camera",       # no parent of a node
    "a/./camera",      # '.' only allowed as the first chunk
    "a/../camera",
    "./",              # empty trailing chunk
    "a//b",
    "a/$/b",           # forbidden zenoh character
])
def test_resolve_topic_rejects_invalid(node_a, topic):
    with pytest.raises(ValueError):
        node_a.resolve_topic(topic)


def test_resolve_topic_multi_chunk_base_topic():
    session = Session(config=zenoh_test_config(), base_topic="fleet/drone1")
    assert session.resolve_topic("./camera") == "fleet/drone1/camera"


# ── Objects ───────────────────────────────────────────────────────────────────

def test_relative_and_bare_authoritative_topics_are_equivalent(node_a):
    """'./camera' on an authoritative object lands where bare 'camera' always has."""
    relative = RelCamera("./cam_equiv", synq_authoritive=True, synq_session=node_a)
    assert relative.synq_absolute_path == "rel_node_a/cam_equiv"
    assert relative.synq_topic == "cam_equiv"
    assert relative.synq_base_topic == "rel_node_a"

    bare = RelCamera("cam_bare", synq_authoritive=True, synq_session=node_a)
    assert bare.synq_absolute_path == "rel_node_a/cam_bare"


def test_relative_mirror_on_same_node(node_a):
    """A mirror opened with './x' on the owning node finds the object at <base>/x."""
    obj = RelCamera("./cam_mirror", synq_authoritive=True, synq_session=node_a, exposure=7)
    mirror_session = Session(config=zenoh_test_config(), base_topic="rel_node_a")

    mirror = RelCamera.from_topic("./cam_mirror", session=mirror_session)
    assert mirror.synq_absolute_path == obj.synq_absolute_path
    assert mirror.exposure == 7

    # zenoh drops puts sent before obj's session has a route to the mirror's
    # subscriber, so keep publishing new values until one arrives.
    def bump_and_check():
        obj.exposure += 1
        return mirror.exposure > 7

    assert _wait_for(bump_and_check)


def test_absolute_topic_reaches_relative_object_from_other_node(node_a, node_b):
    """An object created as './x' on node A is reachable from node B by its absolute path."""
    obj = RelCamera("./cam_cross", synq_authoritive=True, synq_session=node_a, exposure=3)
    mirror = RelCamera.from_topic("rel_node_a/cam_cross", session=node_b)
    assert mirror.synq_absolute_path == obj.synq_absolute_path
    assert mirror.exposure == 3


def test_relative_topic_on_other_node_resolves_to_that_node(node_a, node_b):
    """'./x' is relative to the *calling* session: node B's './x' is rel_node_b/x."""
    obj = RelCamera("./cam_other", synq_authoritive=True, synq_session=node_a)  # noqa: F841
    with pytest.raises(Exception):
        RelCamera.from_topic("./cam_other", session=node_b)


def test_relative_non_authoritative_object(node_a):
    """A mirror constructed directly with './x' uses its session's base topic."""
    obj = RelCamera("./cam_direct", synq_authoritive=True, synq_session=node_a)
    mirror_session = Session(config=zenoh_test_config(), base_topic="rel_node_a")
    mirror = RelCamera("./cam_direct", synq_session=mirror_session)
    assert mirror.synq_absolute_path == obj.synq_absolute_path


def test_invalid_object_topic_raises(node_a):
    with pytest.raises(ValueError):
        RelCamera("../cam", synq_authoritive=True, synq_session=node_a)


def test_list_topics_relative_prefix(node_a, node_b):
    on_a = RelCamera("./cam_list", synq_authoritive=True, synq_session=node_a)  # noqa: F841
    on_b = RelCamera("./cam_list", synq_authoritive=True, synq_session=node_b)  # noqa: F841

    def topics():
        return {m["topic"] for m in node_a.list_topics(type_filter="RelCamera", prefix=".")}

    assert _wait_for(lambda: "rel_node_a/cam_list" in topics())
    assert "rel_node_b/cam_list" not in topics()

