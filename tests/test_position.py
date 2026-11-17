"""
Tests for the Position frame tree: geodesy, pose composition, and parent following.
"""

import math

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

from .conftest import wait_for


def _close(a: Offset, b: Offset, tol=1e-6) -> bool:
    return all(math.isclose(p, q, abs_tol=tol) for p, q in ((a.x, b.x), (a.y, b.y), (a.z, b.z)))


# ── Geodesy ───────────────────────────────────────────────────────────────────


def test_from_geodetic_equator_prime_meridian():
    assert _close(Offset.from_geodetic(0, 0, 0), Offset(x=6378137.0))


@pytest.mark.parametrize(
    "lat, lon, height",
    [
        (45.0, -75.0, 100.0),
        (-33.9, 151.2, -30.0),
        (89.9999, 10.0, 2000.0),
        (-90.0, 0.0, 0.0),
        (0.0, 180.0, 400000.0),
    ],
)
def test_geodetic_round_trip(lat, lon, height):
    got_lat, got_lon, got_height = Offset.from_geodetic(lat, lon, height).to_geodetic()
    assert math.isclose(got_lat, lat, abs_tol=1e-9)
    if abs(lat) < 90:  # longitude is arbitrary at the poles
        assert math.isclose(math.remainder(got_lon - lon, 360), 0, abs_tol=1e-9)
    assert math.isclose(got_height, height, abs_tol=1e-3)


def test_ned_to_ecef_at_equator():
    q = Orientation.ned_to_ecef(0, 0)
    assert _close(Offset(x=1).rotate_by(q), Offset(z=1))  # north is +z
    assert _close(Offset(y=1).rotate_by(q), Offset(y=1))  # east is +y
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


def test_point_child_is_placed_but_faces_nowhere():
    parent = Pose(Offset(10, 0, 0), Orientation.from_euler(0, 0, math.pi / 2))
    world = parent.compose(Pose(Offset(1, 0, 0), orientation=None))
    assert _close(world.offset, Offset(10, 1, 0))
    assert world.orientation is None


def test_under_a_point_only_a_zero_offset_is_placeable():
    point = Pose(Offset(10, 0, 0), orientation=None)
    world = point.compose(Pose(orientation=Orientation.from_euler(0, 0, 1.0)))
    assert world == Pose(Offset(10, 0, 0), orientation=None)
    assert point.compose(Pose(Offset(1, 0, 0))) is None


def test_compose_accuracy():
    a = Accuracy(horizontal=3.0, vertical=4.0)
    assert Pose(accuracy=a).compose(Pose()).accuracy == a
    assert Pose().compose(Pose(accuracy=a)).accuracy == a
    assert Pose(accuracy=a).compose(Pose(accuracy=a)).accuracy == Accuracy(
        horizontal=math.sqrt(18), vertical=math.sqrt(32)
    )


def test_point_round_trips_on_the_wire():
    pos = Position("test/pos_point", synq_authoritive=True, relative_to=RootFrame("ECEF"))
    pos.mount = Pose(Offset(1, 2, 3), orientation=None)
    mirror = Position.from_topic(pos.synq_absolute_path)
    assert mirror.pose == Pose(Offset(1, 2, 3), orientation=None)
    pos.close()


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
    child = Position(
        "test/pos_child",
        synq_authoritive=True,
        relative_to=parent.synq_absolute_path,
        mount=Pose(Offset(1, 0, 0)),
    )
    parent.mount = Pose(Offset(10, 0, 0))
    wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(11, 0, 0)))

    parent.mount = Pose(Offset(20, 0, 0), Orientation.from_euler(0, 0, math.pi / 2))
    wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(20, 1, 0)))

    parent.close()
    wait_for(lambda: child.pose is None)


def test_late_child_gets_quiet_parent_pose():
    """A parent that published once, before the child existed, still places the child."""
    parent = Position(
        "test/pos_quiet_parent",
        synq_authoritive=True,
        relative_to=RootFrame("ECEF"),
        mount=Pose(Offset(5, 0, 0)),
    )
    child = Position(
        "./pos_late_child",
        synq_authoritive=True,
        relative_to="./test/pos_quiet_parent",
        mount=Pose(Offset(0, 1, 0)),
    )
    assert parent.pose is not None
    wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(5, 1, 0)))


def test_child_follows_parent_by_link():
    parent = Position(
        "test/pos_link_parent",
        synq_authoritive=True,
        relative_to=RootFrame("ECEF"),
        mount=Pose(Offset(5, 0, 0)),
    )
    child = Position(
        "test/pos_link_child",
        synq_authoritive=True,
        relative_to=Link.to(parent),
        mount=Pose(Offset(0, 1, 0)),
    )
    wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(5, 1, 0)))

    mirror = Position.from_topic(child.synq_absolute_path)
    assert mirror.relative_to == Link.to(parent)


def test_mirror_receives_pose_without_following_parent():
    parent = Position(
        "test/pos_m_parent",
        synq_authoritive=True,
        relative_to=RootFrame("ECEF"),
        mount=Pose(Offset(5, 0, 0)),
    )
    child = Position(
        "test/pos_m_child",
        synq_authoritive=True,
        relative_to=parent.synq_absolute_path,
        mount=Pose(Offset(1, 0, 0)),
    )
    wait_for(lambda: child.pose is not None)

    mirror = Position.from_topic(child.synq_absolute_path)
    assert mirror._parent is None
    assert mirror.pose == child.pose

    parent.mount = Pose(Offset(7, 0, 0))
    wait_for(lambda: mirror.pose is not None and _close(mirror.pose.offset, Offset(8, 0, 0)))


def test_fuse_positions_skips_unknown_poses():
    known = Position(
        "test/pos_fuse_a",
        synq_authoritive=True,
        relative_to=RootFrame("ECEF"),
        mount=Pose(Offset(1, 2, 3), accuracy=Accuracy(1.0, 1.0)),
    )
    unknown = Position("test/pos_fuse_b", synq_authoritive=True, relative_to=RootFrame("ECEF"))
    offset, accuracy = fuse_positions(known, unknown)
    assert _close(offset, Offset(1, 2, 3))
    assert accuracy == Accuracy(1.0, 1.0)

    with pytest.raises(ValueError):
        fuse_positions(unknown)


# ── The rest of the maths ─────────────────────────────────────────────────────


def test_root_frames():
    assert RootFrame.is_known("ECEF") and not RootFrame.is_known("Mars")
    assert RootFrame("Mars") == "Mars"  # unknown frames are allowed, with a warning


def test_arithmetic_with_other_types():
    assert Offset().__add__(1) is NotImplemented
    assert Offset().__sub__(1) is NotImplemented
    assert Orientation().__mul__(1) is NotImplemented
    assert Accuracy().__add__(1) is NotImplemented
    with pytest.raises(ValueError, match="zero quaternion"):
        Orientation(0, 0, 0, 0).normalize()


def test_euler_angles():
    q = Orientation.from_euler(0.1, 0.2, 0.3)
    assert math.isclose(q.roll, 0.1) and math.isclose(q.pitch, 0.2) and math.isclose(q.yaw, 0.3)
    roll, pitch, yaw = q.to_euler(degrees=True)
    assert math.isclose(yaw, math.degrees(0.3))


@pytest.mark.parametrize(
    "matrix, expected",
    [
        (((1, 0, 0), (0, -1, 0), (0, 0, -1)), Orientation(0, 1, 0, 0)),
        (((-1, 0, 0), (0, 1, 0), (0, 0, -1)), Orientation(0, 0, 1, 0)),
        (((-1, 0, 0), (0, -1, 0), (0, 0, 1)), Orientation(0, 0, 0, 1)),
    ],
)
def test_half_turns_from_a_matrix(matrix, expected):
    assert Orientation._from_matrix(matrix) == expected


def test_accuracy_conversions():
    a = Accuracy(horizontal=2.0, vertical=3.0)
    h, v = a.at_confidence(0.9)
    assert math.isclose(h, 2.0) and math.isclose(v, 3.0)
    ce, le = a.to_cot()
    assert math.isclose(ce, 2.0 / Accuracy._H_FACTOR) and math.isclose(le, 3.0 / Accuracy._V_FACTOR)
    back = Accuracy.from_cot(ce, le)
    assert math.isclose(back.horizontal, 2.0) and math.isclose(back.vertical, 3.0)
    assert Accuracy().at_confidence(0.5) == (None, None)
    assert Accuracy().to_cot() == (None, None)
    assert Accuracy.from_cot(None, None) == Accuracy()


def test_fuse_positions_without_accuracy():
    a = Position("test/pos_fuse_c", synq_authoritive=True, relative_to=RootFrame("ECEF"))
    b = Position("test/pos_fuse_d", synq_authoritive=True, relative_to=RootFrame("ECEF"))
    a.mount, b.mount = Pose(Offset(0, 0, 0)), Pose(Offset(2, 4, 6))
    offset, accuracy = fuse_positions(a, b)
    assert _close(offset, Offset(1, 2, 3)) and accuracy == Accuracy()


# ── Following the parent ──────────────────────────────────────────────────────


def test_a_child_follows_a_new_parent_and_a_root_frame():
    first = Position(
        "test/pos_first",
        synq_authoritive=True,
        relative_to=RootFrame("ECEF"),
        mount=Pose(Offset(1, 0, 0)),
    )
    second = Position(
        "test/pos_second",
        synq_authoritive=True,
        relative_to=RootFrame("ECEF"),
        mount=Pose(Offset(2, 0, 0)),
    )
    child = Position(
        "test/pos_moving",
        synq_authoritive=True,
        relative_to=Link.to(first),
        mount=Pose(Offset(0, 1, 0)),
    )
    wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(1, 1, 0)))
    child.relative_to = Link.to(second)
    wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(2, 1, 0)))
    child.relative_to = RootFrame("ECEF")
    assert child.pose == child.mount and child._parent is None
    child.close()
    child.close()


def test_a_parent_that_comes_back(open_session):
    """The parent's owner goes away, and another process serves it again."""
    elsewhere = open_session("host")
    parent = Position(
        "test/pos_reborn",
        synq_authoritive=True,
        synq_session=elsewhere,
        relative_to=RootFrame("ECEF"),
        mount=Pose(Offset(3, 0, 0)),
    )
    child = Position(
        "test/pos_orphaned",
        synq_authoritive=True,
        relative_to="host/test/pos_reborn",
        mount=Pose(Offset(0, 1, 0)),
    )
    wait_for(lambda: child.pose is not None)
    parent.close()
    wait_for(lambda: child.pose is None)
    parent = Position(
        "test/pos_reborn",
        synq_authoritive=True,
        synq_session=elsewhere,
        relative_to=RootFrame("ECEF"),
        mount=Pose(Offset(3, 0, 0)),
    )
    wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(3, 1, 0)))


def test_a_child_whose_parent_is_a_subclass():
    from dataclasses import dataclass

    @dataclass
    class Drone(Position):
        synq_type_id = "6c0a3f0e-0000-4000-8000-0000000000d1:1"
        armed: bool = False

    parent = Drone(
        "test/pos_drone",
        synq_authoritive=True,
        relative_to=RootFrame("ECEF"),
        mount=Pose(Offset(4, 0, 0)),
    )
    child = Position(
        "test/pos_payload",
        synq_authoritive=True,
        relative_to=Link.to(parent),
        mount=Pose(Offset(0, 0, 1)),
    )
    wait_for(lambda: child.pose is not None and _close(child.pose.offset, Offset(4, 0, 1)))
