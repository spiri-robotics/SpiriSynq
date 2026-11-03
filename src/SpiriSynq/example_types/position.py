"""
Positioned objects, as a tree of frames rooted in a :class:`RootFrame`.

Frame conventions:

- **ECEF** (the usual root): metres; x through the equator at the prime
  meridian, y through the equator at 90°E, z through the north pole.
- **Body frames are FRD**: x forward, y right, z down -- MAVLink's and
  aerospace's convention, so autopilot attitudes go in without conversion. A
  camera mounted 10 cm ahead of a drone's centre is ``Offset(x=0.1)``.
- **Local level frames are NED** (x north, y east, z down);
  :meth:`Orientation.ned_to_ecef` places one on the globe.
- An :class:`Orientation` rotates vectors from the child's frame into its
  parent's, so ``child.offset.rotate_by(parent.orientation)`` is the child's
  offset expressed in the parent's parent.
"""

from typing import TypeAlias, Literal, ClassVar, Self, get_args
from psygnal import SignalGroupDescriptor
from SpiriSynq.syncable_objects import SyncableObject, WeakMethodProxy
from SpiriSynq.serializer import load_untyped
from SpiriSynq.link import Link
from SpiriSynq.session import Session
from dataclasses import dataclass, field
import math
import zenoh

# WGS84 ellipsoid
_WGS84_A = 6378137.0                  # semi-major axis, m
_WGS84_F = 1 / 298.257223563          # flattening
_WGS84_E2 = _WGS84_F * (2 - _WGS84_F)  # first eccentricity squared


def _erfinv(p: float) -> float:
    """Inverse error function via rational approximation + one Halley iteration."""
    a = 0.147
    ln1mp2 = math.log(1 - p * p)
    c = 2 / (math.pi * a) + ln1mp2 / 2
    x = math.copysign(math.sqrt(math.sqrt(c * c - ln1mp2 / a) - c), p)
    f = math.erf(x) - p
    fp = 2 / math.sqrt(math.pi) * math.exp(-(x * x))
    return x - f / (fp + x * f)
from loguru import logger

class RootFrame(str):
    """A string identifying a known root coordinate frame."""

    yaml_tag = "!RootFrame"
    _KNOWN: ClassVar[frozenset[str]] = frozenset({"ECEF", "unknown", "error"})

    def __new__(cls, value: str) -> "RootFrame":
        if value not in cls._KNOWN:
            logger.warning(f"Unrecognized root frame: {value!r}. Known: {sorted(cls._KNOWN)}")
        return super().__new__(cls, value)

    @classmethod
    def is_known(cls, name: str) -> bool:
        return name in cls._KNOWN

    @classmethod
    def to_yaml(cls, representer, data):
        return representer.represent_scalar(cls.yaml_tag, str(data))

    @classmethod
    def from_yaml(cls, constructor, node):
        return cls(constructor.construct_scalar(node))


SpatialFrame: TypeAlias = RootFrame | Link | str

@dataclass(frozen=True)
class Offset:
    """A displacement in metres along the axes of whatever frame holds it."""
    x: float = 0
    y: float = 0
    z: float = 0

    @classmethod
    def from_geodetic(cls, lat: float, lon: float, height: float) -> "Offset":
        """ECEF offset of a WGS84 point.

        *lat*/*lon* in degrees; *height* in metres above the ellipsoid. MAVLink
        altitudes are mostly MSL: add the geoid separation first (e.g.
        ``GPS_RAW_INT.alt_ellipsoid - alt``).
        """
        phi, lam = math.radians(lat), math.radians(lon)
        n = _WGS84_A / math.sqrt(1 - _WGS84_E2 * math.sin(phi) ** 2)
        return cls(
            x=(n + height) * math.cos(phi) * math.cos(lam),
            y=(n + height) * math.cos(phi) * math.sin(lam),
            z=(n * (1 - _WGS84_E2) + height) * math.sin(phi),
        )

    def to_geodetic(self) -> tuple[float, float, float]:
        """Inverse of :meth:`from_geodetic` for an ECEF offset: (lat°, lon°, height above ellipsoid in m)."""
        p = math.hypot(self.x, self.y)
        lon = math.atan2(self.y, self.x)
        lat = math.atan2(self.z, p * (1 - _WGS84_E2))
        # Fixed-point iteration; error shrinks by ~e² per step, so sub-mm well within 10.
        for _ in range(10):
            n = _WGS84_A / math.sqrt(1 - _WGS84_E2 * math.sin(lat) ** 2)
            lat = math.atan2(self.z + _WGS84_E2 * n * math.sin(lat), p)
        # This form of the height stays well-conditioned at the poles, unlike p/cos(lat) - N.
        height = (
            p * math.cos(lat)
            + self.z * math.sin(lat)
            - _WGS84_A * math.sqrt(1 - _WGS84_E2 * math.sin(lat) ** 2)
        )
        return math.degrees(lat), math.degrees(lon), height

    def rotate_by(self, orientation: "Orientation") -> "Offset":
        """Return a new Offset rotated by the given orientation (quaternion)."""
        q = orientation.normalize()
        vx, vy, vz = self.x, self.y, self.z
        w, qx, qy, qz = q.w, q.x, q.y, q.z

        # Quaternion-vector rotation: v' = q * v * q_conj (expanded)
        return Offset(
            x=(1 - 2*(qy**2 + qz**2)) * vx + 2*(qx*qy - qz*w) * vy + 2*(qx*qz + qy*w) * vz,
            y=2*(qx*qy + qz*w) * vx + (1 - 2*(qx**2 + qz**2)) * vy + 2*(qy*qz - qx*w) * vz,
            z=2*(qx*qz - qy*w) * vx + 2*(qy*qz + qx*w) * vy + (1 - 2*(qx**2 + qy**2)) * vz,
        )

    def __add__(self, other: "Offset") -> "Offset":
        if not isinstance(other, Offset):
            return NotImplemented
        return Offset(x=self.x + other.x, y=self.y + other.y, z=self.z + other.z)

    def __sub__(self, other: "Offset") -> "Offset":
        if not isinstance(other, Offset):
            return NotImplemented
        return Offset(x=self.x - other.x, y=self.y - other.y, z=self.z - other.z)


@dataclass(frozen=True)
class Orientation:
    w: float = 1  # Scalar (real) component
    x: float = 0  # Vector (imaginary) component X
    y: float = 0  # Vector (imaginary) component Y
    z: float = 0  # Vector (imaginary) component Z

    def normalize(self) -> "Orientation":
        mag = math.sqrt(self.w**2 + self.x**2 + self.y**2 + self.z**2)
        if mag == 0:
            raise ValueError("Cannot normalize a zero quaternion.")
        return Orientation(self.w / mag, self.x / mag, self.y / mag, self.z / mag)

    def __mul__(self, other: "Orientation") -> "Orientation":
        """Hamilton product of two quaternions — returns a new Orientation."""
        if not isinstance(other, Orientation):
            return NotImplemented
        w1, x1, y1, z1 = self.w, self.x, self.y, self.z
        w2, x2, y2, z2 = other.w, other.x, other.y, other.z
        return Orientation(
            w=w1*w2 - x1*x2 - y1*y2 - z1*z2,
            x=w1*x2 + x1*w2 + y1*z2 - z1*y2,
            y=w1*y2 - x1*z2 + y1*w2 + z1*x2,
            z=w1*z2 + x1*y2 - y1*x2 + z1*w2,
        )

    def to_euler(self, degrees: bool = False) -> tuple[float, float, float]:
        """
        Returns (roll, pitch, yaw) in radians (or degrees if degrees=True).
        Uses ZYX intrinsic convention (yaw -> pitch -> roll).
        """
        q = self.normalize()

        # Roll (X-axis)
        roll = math.atan2(2.0 * (q.w * q.x + q.y * q.z),
                          1.0 - 2.0 * (q.x**2 + q.y**2))

        # Pitch (Y-axis) — clamped to avoid NaN at gimbal lock
        pitch = math.asin(max(-1.0, min(1.0, 2.0 * (q.w * q.y - q.z * q.x))))

        # Yaw (Z-axis)
        yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                         1.0 - 2.0 * (q.y**2 + q.z**2))

        if degrees:
            return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)
        return roll, pitch, yaw

    @property
    def roll(self) -> float:
        return self.to_euler()[0]

    @property
    def pitch(self) -> float:
        return self.to_euler()[1]

    @property
    def yaw(self) -> float:
        return self.to_euler()[2]

    @classmethod
    def from_euler(cls, roll: float, pitch: float, yaw: float) -> "Orientation":
        """Construct from roll, pitch, yaw in radians. ZYX intrinsic convention."""
        cr, sr = math.cos(roll / 2), math.sin(roll / 2)
        cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
        cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)

        return cls(
            w=cr * cp * cy + sr * sp * sy,
            x=sr * cp * cy - cr * sp * sy,
            y=cr * sp * cy + sr * cp * sy,
            z=cr * cp * sy - sr * sp * cy,
        )

    @classmethod
    def ned_to_ecef(cls, lat: float, lon: float) -> "Orientation":
        """Orientation of the local NED frame at *lat*/*lon* (degrees) within ECEF.

        Use it as the orientation of a NED frame's :class:`Pose`, such as an
        autopilot's EKF origin, so children can mount in NED.
        """
        phi, lam = math.radians(lat), math.radians(lon)
        sp, cp, sl, cl = math.sin(phi), math.cos(phi), math.sin(lam), math.cos(lam)
        # Columns: the north, east and down unit vectors in ECEF.
        return cls._from_matrix((
            (-sp * cl, -sl, -cp * cl),
            (-sp * sl, cl, -cp * sl),
            (cp, 0.0, -sp),
        ))

    @classmethod
    def _from_matrix(cls, m) -> "Orientation":
        """Quaternion of a 3x3 rotation matrix (rows of columns), via Shepperd's method."""
        trace = m[0][0] + m[1][1] + m[2][2]
        if trace > 0:
            s = math.sqrt(trace + 1) * 2
            return cls(s / 4, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
        if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
            s = math.sqrt(1 + m[0][0] - m[1][1] - m[2][2]) * 2
            return cls((m[2][1] - m[1][2]) / s, s / 4, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s)
        if m[1][1] > m[2][2]:
            s = math.sqrt(1 + m[1][1] - m[0][0] - m[2][2]) * 2
            return cls((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, s / 4, (m[1][2] + m[2][1]) / s)
        s = math.sqrt(1 + m[2][2] - m[0][0] - m[1][1]) * 2
        return cls((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, s / 4)

@dataclass(frozen=True)
class Accuracy:
    """
    Positional accuracy expressed as error bounds at 90% confidence.

    There should be a 90% chance the object is inside of this cylinder.

    Stored as CEP90/LEP90. 
    
    We break from ATAK/CoT here. On CoT ce/le uses 1-sigma (one standard deviation),
    which corresponds to ~39% confidence for horizontal (2D Rayleigh) and
    ~68% for vertical (1D Gaussian) — use at_confidence() to convert.

    The intent behind this difference is that it's easy to convert, and
    provides developers with an easy and intuitive understanding of their
    confidence bounds. It roughly tracks to an intuitive "The object is here".


    horizontal: radius in metres of the circle within which the true
                horizontal position lies with 90% probability (CEP90).
    vertical:   half-height in metres of the vertical band within which
                the true altitude lies with 90% probability (LEP90).
    """
    horizontal: float | None = None
    vertical: float | None = None

    # Rayleigh σ multiplier for CEP90 → 1σ
    _H_FACTOR = math.sqrt(-2 * math.log(0.1))  # ≈ 2.146
    # Gaussian σ multiplier for LEP90 → 1σ
    _V_FACTOR = math.sqrt(2) * _erfinv(0.9)     # ≈ 1.6449

    def at_confidence(self, confidence: float) -> tuple[float | None, float | None]:
        """Return (horizontal, vertical) bounds rescaled to a different confidence level.

        Stored values are CEP90/LEP90. Horizontal uses a 2D Rayleigh distribution;
        vertical uses a 1D symmetric Gaussian.
        """
        h_scale = math.sqrt(math.log(1 - confidence) / math.log(0.1))
        v_scale = _erfinv(confidence) / _erfinv(0.9)
        h = self.horizontal * h_scale if self.horizontal is not None else None
        v = self.vertical * v_scale if self.vertical is not None else None
        return h, v

    def to_cot(self) -> tuple[float | None, float | None]:
        """Return (ce, le) in CoT 1-sigma convention."""
        ce = self.horizontal / self._H_FACTOR if self.horizontal is not None else None
        le = self.vertical / self._V_FACTOR if self.vertical is not None else None
        return ce, le

    @classmethod
    def from_cot(cls, ce: float | None, le: float | None) -> "Accuracy":
        """Construct from CoT 1-sigma (ce, le) values."""
        h = ce * cls._H_FACTOR if ce is not None else None
        v = le * cls._V_FACTOR if le is not None else None
        return cls(horizontal=h, vertical=v)

    def __add__(self, other: "Accuracy") -> "Accuracy":
        """Combine independent error sources on the same observation (additive).

        Use this when stacking independent error contributions — e.g. sensor
        noise + GPS noise. The result is LESS certain than either input alone.
        """
        if not isinstance(other, Accuracy):
            return NotImplemented
        h = math.sqrt(self.horizontal ** 2 + other.horizontal ** 2) \
            if self.horizontal is not None and other.horizontal is not None else None
        v = math.sqrt(self.vertical ** 2 + other.vertical ** 2) \
            if self.vertical is not None and other.vertical is not None else None
        return Accuracy(horizontal=h, vertical=v)



@dataclass(frozen=True)
class Pose:
    """Where something is and which way it faces, within some frame.

    ``orientation=None`` makes it a point: something with a place but no
    facing, such as a region of interest. The default, the identity, means
    aligned with the parent frame.

    One value, published as one put: a subscriber never pairs the offset from
    one update with the orientation from another.
    """
    offset: Offset = field(default_factory=Offset)
    orientation: Orientation | None = field(default_factory=Orientation)
    accuracy: Accuracy | None = None

    def compose(self, child: "Pose") -> "Pose | None":
        """Place *child*, given in this pose's local frame, into the frame this pose is in.

        The standard rigid-body chain, T_child = T_parent ∘ T_mount:
            offset = parent.offset + child.offset.rotate_by(parent.orientation)
            orientation = parent.orientation * child.orientation
        Accuracies combine in quadrature; whichever is known propagates.

        A point has no axes to measure from: under one, a child at a zero
        offset is a point at the same place, and any other child can't be
        placed, so this returns None. A point child stays a point.
        """
        if self.accuracy is not None and child.accuracy is not None:
            accuracy = self.accuracy + child.accuracy
        else:
            accuracy = self.accuracy or child.accuracy
        if self.orientation is None:
            if child.offset != Offset():
                return None
            return Pose(offset=self.offset, orientation=None, accuracy=accuracy)
        return Pose(
            offset=self.offset + child.offset.rotate_by(self.orientation),
            orientation=None if child.orientation is None else self.orientation * child.orientation,
            accuracy=accuracy,
        )

    @classmethod
    def from_plain(cls, data: dict | None) -> "Pose | None":
        """Build from the tag-stripped mapping ``load_untyped`` gives for a Pose."""
        if not data:
            return None
        accuracy = data.get("accuracy")
        orientation = data.get("orientation")
        return cls(
            offset=Offset(**(data.get("offset") or {})),
            orientation=Orientation(**orientation) if orientation else None,
            accuracy=Accuracy(**accuracy) if accuracy else None,
        )


@dataclass
class Position(SyncableObject):
    """
    Something with a place in the world: a node in a tree of frames.

    ``relative_to`` names the parent: a :class:`RootFrame`, or the topic of
    another Position: a :class:`~SpiriSynq.link.Link`, or a topic string
    (absolute, or relative like ``./origin``). Prefer a Link: a relative topic
    means something different on every node that receives it. ``mount`` is
    this object's pose in its parent's frame -- the value you set. ``pose`` is
    the resulting pose in the root frame, which the authoritative node computes
    and publishes; mirrors just receive it.

    ``pose`` is None until it can be computed: without a mount, or while the
    parent's own pose is unknown. An unknown parent is never treated as the
    identity, which would put the object near the root frame's origin.
    """
    relative_to: SpatialFrame = RootFrame("unknown")
    mount: Pose | None = None
    pose: Pose | None = None
    _parent_subscriber: zenoh.Subscriber | None = None
    _parent_pose: Pose | None = None
    _parent_live: bool = False
    _parent_generation: int = 0

    def __post_init__(self):
        super().__post_init__()
        if not self.synq_authoritive:
            return
        self.events.relative_to.connect(self._follow_parent)
        self.events.mount.connect(self._on_mount_change)
        self._follow_parent(self.relative_to)

    def _on_mount_change(self, _):
        self._update_pos()

    def _unfollow_parent(self):
        if self._parent_subscriber is None:
            return
        try:
            self._parent_subscriber.undeclare()
        except zenoh.ZError:
            logger.trace(f"Subscriber already undeclared (session reset?): {self._parent_subscriber!r}")
        self._parent_subscriber = None

    def _follow_parent(self, frame: SpatialFrame):
        self._unfollow_parent()
        self._parent_generation += 1
        self._parent_pose = None
        self._parent_live = False

        if isinstance(frame, RootFrame):
            logger.trace(f"New frame isn't relative: {frame}")
            self._update_pos()
            return

        if self.synq_session is None:
            raise RuntimeError("synq_session is required to subscribe to relative frames")

        parent = frame.topic if isinstance(frame, Link) else self.synq_session.resolve_topic(frame)
        z_session = self.synq_session.zenoh_session
        self._parent_subscriber = z_session.declare_subscriber(
            f"{parent}/pose", WeakMethodProxy(self._on_parent_pose_sample)
        )
        self._update_pos()

        # A parent such as an EKF origin may publish once and then go quiet, so
        # ask for its current state rather than waiting for its next put. The
        # reply is untyped: the parent may be a subclass this process never imported.
        generation = self._parent_generation
        z_session.get(
            f"{parent}/sr_rehydrate",
            lambda reply: self._on_parent_rehydrate(reply, generation),
        )

    def _on_parent_pose_sample(self, sample: zenoh.Sample):
        """Callback fired by zenoh when the parent publishes its pose, or is deleted."""
        if sample.kind == zenoh.SampleKind.DELETE:
            new_pose = None  # parent closed
        else:
            if self.synq_session is None:
                raise RuntimeError("synq_session is required to deserialize pose")
            new_pose = self.synq_session.type_registry.load(sample.payload.to_string())
            if new_pose is not None and not isinstance(new_pose, Pose):
                raise TypeError(f"{new_pose} is {type(new_pose)}, not {Pose}")
        self._parent_live = True
        self._parent_pose = new_pose
        self._update_pos()

    def _on_parent_rehydrate(self, reply: zenoh.Reply, generation: int):
        # A live sample is newer than this snapshot; a new frame makes it irrelevant.
        if generation != self._parent_generation or self._parent_live:
            return
        if not reply.ok:
            logger.trace(f"Parent {self.relative_to} didn't rehydrate: {reply.err}")
            return
        state = load_untyped(reply.ok.payload.to_string())
        if not isinstance(state, dict):
            return
        self._parent_pose = Pose.from_plain(state.get("pose"))
        self._update_pos()

    def _update_pos(self):
        """Recompute ``pose`` from the mount and the parent's root-frame pose."""
        if self.mount is None:
            self.pose = None
        elif isinstance(self.relative_to, RootFrame):
            self.pose = self.mount
        elif self._parent_pose is None:
            self.pose = None
        else:
            self.pose = self._parent_pose.compose(self.mount)

    def close(self):
        self._unfollow_parent()
        super().close()


def fuse_positions(*positions: Position) -> tuple[Offset, Accuracy]:
    """Fuse N independent position observations into a single best estimate.

    Uses inverse-variance weighting when horizontal/vertical accuracies are
    known, falling back to a simple mean otherwise. More observations with
    known accuracy produce a tighter result. Positions whose pose is unknown
    are skipped.

    Returns (offset, accuracy) where either component may have None fields
    if no accuracy information was available for that axis.
    """
    poses = [p.pose for p in positions if p.pose is not None]
    if not poses:
        raise ValueError("At least one position with a known pose is required")

    offsets = [pose.offset for pose in poses]

    # Pairs of (offset, h_accuracy) for poses that have horizontal accuracy
    h_known = [(pose.offset, pose.accuracy.horizontal)
               for pose in poses
               if pose.accuracy is not None and pose.accuracy.horizontal is not None]

    # Pairs of (offset, v_accuracy) for poses that have vertical accuracy
    v_known = [(pose.offset, pose.accuracy.vertical)
               for pose in poses
               if pose.accuracy is not None and pose.accuracy.vertical is not None]

    if h_known:
        inv_var = [1.0 / h ** 2 for _, h in h_known]
        w_total = sum(inv_var)
        x = sum(off.x * w for (off, _), w in zip(h_known, inv_var)) / w_total
        y = sum(off.y * w for (off, _), w in zip(h_known, inv_var)) / w_total
        h_acc = 1.0 / math.sqrt(w_total)
    else:
        x = sum(off.x for off in offsets) / len(offsets)
        y = sum(off.y for off in offsets) / len(offsets)
        h_acc = None

    if v_known:
        inv_var = [1.0 / v ** 2 for _, v in v_known]
        w_total = sum(inv_var)
        z = sum(off.z * w for (off, _), w in zip(v_known, inv_var)) / w_total
        v_acc = 1.0 / math.sqrt(w_total)
    else:
        z = sum(off.z for off in offsets) / len(offsets)
        v_acc = None

    return Offset(x=x, y=y, z=z), Accuracy(horizontal=h_acc, vertical=v_acc)

