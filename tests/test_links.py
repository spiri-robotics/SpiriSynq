"""Links: fields holding other syncable objects, and mirrors following them."""

from dataclasses import dataclass, field

import pytest

from SpiriSynq import _core
from SpiriSynq.link import Link, ObjectNotFound, check_topic
from SpiriSynq.syncable_objects import SyncableObject

from .conftest import wait_for


@dataclass
class Camera(SyncableObject):
    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000003:1"
    exposure: float = 0.01


@dataclass
class Robot(SyncableObject):
    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000004:1"
    camera: Camera | None = None
    cameras: dict[str, Camera] = field(default_factory=dict)
    spares: list[Camera] = field(default_factory=list)
    anything: Link | None = None


def test_a_mirror_follows_links(other):
    robot = Robot("robot", synq_authoritive=True)
    camera = Camera("robot/camera", synq_authoritive=True, exposure=0.02)
    robot.camera = camera
    robot.cameras = {"front": camera}
    robot.spares = [camera]
    robot.anything = Link.to(camera)
    mirror = Robot.from_topic("host/robot", session=other)
    assert isinstance(mirror.camera, Camera) and not mirror.camera.synq_authoritive
    assert mirror.cameras["front"] is mirror.camera
    assert mirror.spares[0] is mirror.camera
    assert mirror.anything == Link("host/robot/camera")
    wait_for(lambda: mirror.camera.exposure == 0.02)
    camera.exposure = 0.5
    wait_for(lambda: mirror.camera.exposure == 0.5)
    robot.camera = Link("host/robot/other")
    wait_for(lambda: isinstance(mirror.camera, ObjectNotFound))
    assert mirror.camera.topic == "host/robot/other"
    with pytest.raises(TimeoutError):
        mirror.camera.rebind(timeout=0.3)
    second = Camera("robot/other", synq_authoritive=True, exposure=0.7)
    found = mirror.camera.rebind()
    assert mirror.camera is found and found.exposure == 0.7
    second.close()


def test_links_can_be_left_unfollowed(other):
    robot = Robot("robot", synq_authoritive=True)
    camera = Camera("robot/camera", synq_authoritive=True, exposure=0.3)
    robot.camera = camera
    mirror = Robot.from_topic("host/robot", session=other, follow_links=False)
    assert mirror.camera == Link("host/robot/camera")
    resolved = mirror.camera.resolve(Camera, session=other)
    assert resolved.exposure == 0.3


def test_a_link_to_a_closed_mirror_is_made_again(other):
    robot = Robot("robot", synq_authoritive=True)
    camera = Camera("robot/camera", synq_authoritive=True)
    robot.camera = camera
    mirror = Robot.from_topic("host/robot", session=other)
    first = mirror.camera
    first.close()
    robot.camera = None
    wait_for(lambda: mirror.camera is None)
    robot.camera = camera
    wait_for(lambda: mirror.camera is not None)
    assert mirror.camera is not first


def test_links_must_be_absolute_topics():
    for bad in ["./camera", "a/../b", "", "a/*", "a//b"]:
        with pytest.raises(ValueError):
            Link(bad)
    assert check_topic("a/b") == "a/b"
    with pytest.raises(ValueError, match="can't be in a key"):
        check_topic("a$b")


def test_a_not_found_for_a_mirror_already_replaced(other):
    robot = Robot("robot", synq_authoritive=True)
    mirror = Robot.from_topic("host/robot", session=other)
    stray = Camera.from_topic("host/nowhere", session=other, wait=False)
    mirror._synq_not_found("camera", stray)  # not one of its links: nothing to do
    assert mirror.camera is None
    _ = (_core, robot)


def test_links_in_collections_that_cant_be_followed(other):
    robot = Robot("robot", synq_authoritive=True)
    camera = Camera("robot/camera", synq_authoritive=True)
    robot.spares = [Link("host/missing/a"), camera]
    robot.cameras = {"x": Link("host/missing/b")}
    mirror = Robot.from_topic("host/robot", session=other)
    wait_for(lambda: isinstance(mirror.spares[0], ObjectNotFound))
    wait_for(lambda: isinstance(mirror.cameras["x"], ObjectNotFound))
    assert isinstance(mirror.spares[1], Camera)
