"""
Tests for the Position frame tree: geodesy, pose composition, and parent following.
"""

import math
import time

import pytest

from SpiriSynq.example_types.position import (
    Accuracy,
    Offset,
    Orientation,
    Pose,
    Position,
    RootFrame,
    fuse_positions,
)
from SpiriSynq.link import Link


def _wait_for(predicate, timeout=3.0, interval=0.01):
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _close(a: Offset, b: Offset, tol=1e-6) -> bool:
    return all(math.isclose(p, q, abs_tol=tol) for p, q in ((a.x, b.x), (a.y, b.y), (a.z, b.z)))


# ── Geodesy ───────────────────────────────────────────────────────────────────

def test_from_geodetic_equator_prime_meridian():
    assert _close(Offset.from_geodetic(0, 0, 0), Offset(x=6378137.0))


@pytest.mark.parametrize("lat, lon, height", [
    (45.0, -75.0, 100.0),
    (-33.9, 151.2, -30.0),
    (89.9999, 10.0, 2000.0),
    (-90.0, 0.0, 0.0),
    (0.0, 180.0, 400000.0),
])
def test_geodetic_round_trip(lat, lon, height):
    got_lat, got_lon, got_height = Offset.from_geodetic(lat, lon, height).to_geodetic()
    assert math.isclose(got_lat, lat, abs_tol=1e-9)
    if abs(lat) < 90:  # longitude is arbitrary at the poles
        assert math.isclose(math.remainder(got_lon - lon, 360), 0, abs_tol=1e-9)
    assert math.isclose(got_height, height, abs_tol=1e-3)


def test_ned_to_ecef_at_equator():
    q = Orientation.ned_to_ecef(0, 0)
    assert _close(Offset(x=1).rotate_by(q), Offset(z=1))   # north is +z
    assert _close(Offset(y=1).rotate_by(q), Offset(y=1))   # east is +y
    assert _close(Offset(z=1).rotate_by(q), Offset(x=-1))  # down is toward the centre


def test_ned_to_ecef_down_points_at_ellipsoid_normal():
    lat, lon = 45.0, -75.0
    down = Offset(z=1).rotate_by(Orientation.ned_to_ecef(lat, lon))
    # A point 1 m below on the ellipsoid normal
    below = Offset.from_geodetic(lat, lon, -1) - Offset.from_geodetic(lat, lon, 0)
    assert _close(down, below, tol=1e-6)


# ── Pose composition ──────────────────────────────────────────────────────────

def test_compose_rotates_child_offset_into_parent_frame():
    parent = Pose(Offset(10, 0, 0), Orientation.from_euler(0, 0, math.pi / 2))
    child = Pose(Offset(1, 0, 0))
    world = parent.compose(child)
    assert _close(world.offset, Offset(10, 1, 0))
    assert math.isclose(world.orientation.yaw, math.pi / 2)


def test_compose_accuracy():
    a = Accuracy(horizontal=3.0, vertical=4.0)
    assert Pose(accuracy=a).compose(Pose()).accuracy == a
    assert Pose().compose(Pose(accuracy=a)).accuracy == a
    assert Pose(accuracy=a).compose(Pose(accuracy=a)).accuracy == Accuracy(
        horizontal=math.sqrt(18), vertical=math.sqrt(32)
    )


def test_pose_from_plain():
    assert Pose.from_plain(None) is None
    pose = Pose.from_plain({
        "offset": {"x": 1, "y": 2, "z": 3},
        "orientation": {"w": 1, "x": 0, "y": 0, "z": 0},
        "accuracy": {"horizontal": 1.5, "vertical": None},
    })
    assert pose == Pose(Offset(1, 2, 3), Orientation(), Accuracy(horizontal=1.5))


# ── Position tree ─────────────────────────────────────────────────────────────

def test_root_frame_pose_is_mount():
    pos = Position("test/pos_root", synq_authoritive=True, relative_to=RootFrame("ECEF"))
    assert pos.pose is None
    pos.mount = Pose(Offset(1, 2, 3))
    assert pos.pose == pos.mount


def test_unknown_parent_gives_no_pose():
    pos = Position("test/pos_orphan", synq_authoritive=True, relative_to="test/pos_nobody_here")
    pos.mount = Pose(Offset(1, 2, 3))
    assert pos.pose is None


def test_child_follows_parent_updates_and_deletion():
    parent = Position("test/pos_parent", synq_authoritive=True, relative_to=RootFrame("ECEF"))
    child = Position("test/pos_child", synq_authoritive=True,
                     relative_to=parent.synq_absolute_path, mount=Pose(Offset(1, 0, 0)))
    assert _wait_for(lambda: child.synq_publisher.matching_status.matching)

    parent.mount = Pose(Offset(10, 0, 0))
    assert _wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(11, 0, 0)))

    parent.mount = Pose(Offset(20, 0, 0), Orientation.from_euler(0, 0, math.pi / 2))
    assert _wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(20, 1, 0)))

    parent.close()
    assert _wait_for(lambda: child.pose is None)


def test_late_child_gets_quiet_parent_pose():
    """A parent that published once, before the child existed, still places the child."""
    parent = Position("test/pos_quiet_parent", synq_authoritive=True,
                      relative_to=RootFrame("ECEF"), mount=Pose(Offset(5, 0, 0)))
    child = Position("./pos_late_child", synq_authoritive=True,
                     relative_to=f"./test/pos_quiet_parent", mount=Pose(Offset(0, 1, 0)))
    assert parent.pose is not None
    assert _wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(5, 1, 0)))


def test_child_follows_parent_by_link():
    parent = Position("test/pos_link_parent", synq_authoritive=True,
                      relative_to=RootFrame("ECEF"), mount=Pose(Offset(5, 0, 0)))
    child = Position("test/pos_link_child", synq_authoritive=True,
                     relative_to=Link.to(parent), mount=Pose(Offset(0, 1, 0)))
    assert _wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(5, 1, 0)))

    mirror = Position.from_topic(child.synq_absolute_path)
    assert mirror.relative_to == Link.to(parent)


def test_mirror_receives_pose_without_following_parent():
    parent = Position("test/pos_m_parent", synq_authoritive=True,
                      relative_to=RootFrame("ECEF"), mount=Pose(Offset(5, 0, 0)))
    child = Position("test/pos_m_child", synq_authoritive=True,
                     relative_to=parent.synq_absolute_path, mount=Pose(Offset(1, 0, 0)))
    assert _wait_for(lambda: child.pose is not None)

    mirror = Position.from_topic(child.synq_absolute_path)
    assert mirror._parent_subscriber is None
    assert mirror.pose == child.pose

    parent.mount = Pose(Offset(7, 0, 0))
    assert _wait_for(lambda: mirror.pose is not None and _close(mirror.pose.offset, Offset(8, 0, 0)))


def test_fuse_positions_skips_unknown_poses():
    known = Position("test/pos_fuse_a", synq_authoritive=True, relative_to=RootFrame("ECEF"),
                     mount=Pose(Offset(1, 2, 3), accuracy=Accuracy(1.0, 1.0)))
    unknown = Position("test/pos_fuse_b", synq_authoritive=True, relative_to=RootFrame("ECEF"))
    offset, accuracy = fuse_positions(known, unknown)
    assert _close(offset, Offset(1, 2, 3))
    assert accuracy == Accuracy(1.0, 1.0)

    with pytest.raises(ValueError):
        fuse_positions(unknown)
