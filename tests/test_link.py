"""Tests for the ``!Link`` reference type."""

import time
from dataclasses import dataclass

import pytest

from SpiriSynq.link import Link
from SpiriSynq.session import Session, current_session
from SpiriSynq.syncable_objects import SyncableObject
from conftest import zenoh_test_config


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


@dataclass
class Camera(SyncableObject):
    exposure: float = 0.0


@dataclass
class Mount(SyncableObject):
    camera: Link | None = None


def test_wire_format_is_a_tagged_scalar():
    registry = current_session.get().type_registry
    assert registry.dumps(Link("myhost/camera")) == "!Link 'myhost/camera'"
    assert registry.load("!Link myhost/camera") == Link("myhost/camera")


def test_builtin_without_registration():
    """Any session decodes a link, even if no field is annotated with it."""
    session = Session(config=zenoh_test_config())
    assert session.type_registry.load("[!Link a/b]") == [Link("a/b")]


@pytest.mark.parametrize("topic", ["./camera", ".", "a/../b", "a/./b", "", "a//b"])
def test_rejects_relative_and_invalid_topics(topic):
    with pytest.raises(ValueError):
        Link(topic)


def test_to_uses_absolute_path():
    cam = Camera("link_test/camera", synq_authoritive=True)
    assert Link.to(cam) == Link(cam.synq_absolute_path)
    assert cam.synq_absolute_path.endswith("/link_test/camera")


def test_resolve_returns_a_mirror():
    cam = Camera("link_test/resolve", synq_authoritive=True, exposure=0.5)
    session_b = Session(config=zenoh_test_config())

    mirror = Link.to(cam).resolve(Camera, session=session_b)
    assert isinstance(mirror, Camera) and mirror.synq_session is session_b
    assert mirror.exposure == 0.5

    # Without a class, the type only needs to be registered already.
    again = Link.to(cam).resolve(session=session_b)
    assert isinstance(again, Camera) and again.exposure == 0.5


def test_link_field_rehydrates_and_syncs():
    cam = Camera("link_test/cam_a", synq_authoritive=True)
    mount = Mount("link_test/mount", synq_authoritive=True, camera=Link.to(cam))

    session_b = Session(config=zenoh_test_config())
    remote = Mount.from_topic(mount.synq_absolute_path, session=session_b)
    assert remote.camera == Link.to(cam)

    other = Camera("link_test/cam_b", synq_authoritive=True)
    assert _wait_for(lambda: mount.synq_publisher.matching_status.matching)
    mount.camera = Link.to(other)
    assert _wait_for(lambda: remote.camera == Link.to(other))
