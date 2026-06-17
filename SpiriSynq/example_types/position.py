from typing import TypeAlias, Literal, ClassVar, Self, get_args
from psygnal import SignalGroupDescriptor
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session
from dataclasses import dataclass, field
import math
import zenoh
from loguru import logger

class RootFrame(str):
    """A string identifying a known root coordinate frame."""

    _KNOWN: ClassVar[frozenset[str]] = frozenset({"ECEF", "unknown", "error"})

    def __new__(cls, value: str) -> "RootFrame":
        if value not in cls._KNOWN:
            logger.warning(f"Unrecognized root frame: {value!r}. Known: {sorted(cls._KNOWN)}")
        return super().__new__(cls, value)

    @classmethod
    def is_known(cls, name: str) -> bool:
        return name in cls._KNOWN


SpatialFrame: TypeAlias = RootFrame | str

@dataclass(frozen=True)
class Offset:
    x: float = 0  # X-axis: Equator at Prime Meridian
    y: float = 0  # Y-axis: Equator at 90° East longitude
    z: float = 0  # Z-axis: North Pole direction

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


@dataclass(frozen=True)
class Accuracy:
    """
    Positional accuracy expressed as error bounds at 90% confidence.

    Mirrors the ATAK CoT ce/le convention but uses 90th-percentile bounds
    (CEP90/LEP90) rather than the CoT standard 50%.

    horizontal: radius in metres of the circle within which the true
                horizontal position lies with 90% probability (CEP90).
    vertical:   half-height in metres of the vertical band within which
                the true altitude lies with 90% probability (LEP90).
    """
    horizontal: float | None = None
    vertical: float | None = None

    def __add__(self, other: "Accuracy") -> "Accuracy":
        """Combine two independent accuracy estimates by adding errors in quadrature (RSS)."""
        if not isinstance(other, Accuracy):
            return NotImplemented
        h = math.sqrt(self.horizontal ** 2 + other.horizontal ** 2) if self.horizontal is not None and other.horizontal is not None else None
        v = math.sqrt(self.vertical ** 2 + other.vertical ** 2) if self.vertical is not None and other.vertical is not None else None
        return Accuracy(horizontal=h, vertical=v)


@dataclass
class Position(SyncableObject):
    """
    A generic object that exists in 3D space relative to another object.

    offset and orientation are the real position in the root (world) frame.
    Mounts describe where the device is relative to its parent's local frame.
    For example, if your robot is X above the center of the earth and your root
    frame is ECEF, you mount the drone X above the root.
    """
    relative_to: SpatialFrame = RootFrame("unknown")
    _relative_subscribers: set[zenoh.Subscriber] = field(default_factory=set)
    _relative_offset: Offset | None = None
    _relative_orientation: Orientation | None = None
    _relative_accuracy: Accuracy | None = None
    offset: Offset | None = None
    orientation: Orientation | None = None
    accuracy: Accuracy | None = None
    # Where the device is relative to its parent (parent's local frame)
    mount_offset: Offset | None = None
    mount_orientation: Orientation | None = None
    mount_accuracy: Accuracy | None = None

    def __post_init__(self):
        super().__post_init__()
        self.events.relative_to.connect(self._update_relative_subscriber)
        self.events.mount_offset.connect(self._on_mount_change)
        self.events.mount_orientation.connect(self._on_mount_change)
        self.events.mount_accuracy.connect(self._on_mount_change)

        # Set up zenoh subscriber for the current frame
        self._update_relative_subscriber(self.relative_to)

    def _on_mount_change(self, _):
        self._update_pos()

    def _update_relative_subscriber(self, frame: SpatialFrame):
        # Clean up all existing subscribers
        for subscriber in self._relative_subscribers:
            subscriber.undeclare()
            logger.trace(f"Undeclared stale subscriber {subscriber}")
        self._relative_subscribers.clear()

        # If this is a root frame, no relative subscription needed
        if isinstance(frame, RootFrame):
            logger.trace(f"New frame isn't relative: {frame}")
            self._relative_offset = None
            self._relative_orientation = None
            self._relative_accuracy = None
            self._update_pos()
            return

        if self.synq_session is None:
            raise RuntimeError("synq_session is required to subscribe to relative frames")

        # Subscribe to offset, orientation, and accuracy on separate key expressions
        offset_sub = self.synq_session.zenoh_session.declare_subscriber(
            f"{self.relative_to}/offset",
            self._on_offset_sample,
        )
        orientation_sub = self.synq_session.zenoh_session.declare_subscriber(
            f"{self.relative_to}/orientation",
            self._on_orientation_sample,
        )
        accuracy_sub = self.synq_session.zenoh_session.declare_subscriber(
            f"{self.relative_to}/accuracy",
            self._on_accuracy_sample,
        )

        self._relative_subscribers = {offset_sub, orientation_sub, accuracy_sub}

    def _on_offset_sample(self, sample: zenoh.Sample):
        """Callback fired by zenoh when the relative frame publishes an offset update."""
        payload = sample.payload.to_string()
        if self.synq_session is None:
            raise RuntimeError("synq_session is required to deserialize offset")

        new_offset = self.synq_session.type_registry.load(payload)
        if not isinstance(new_offset, Offset):
            raise TypeError(f"{new_offset} is {type(new_offset)}, not {Offset}")
        self._relative_offset = new_offset
        self._update_pos()

    def _on_orientation_sample(self, sample: zenoh.Sample):
        """Callback fired by zenoh when the relative frame publishes an orientation update."""
        payload = sample.payload.to_string()
        if self.synq_session is None:
            raise RuntimeError("synq_session is required to deserialize orientation")

        new_orientation = self.synq_session.type_registry.load(payload)
        if not isinstance(new_orientation, Orientation):
            raise TypeError(f"{new_orientation} is {type(new_orientation)}, not {Orientation}")
        self._relative_orientation = new_orientation
        self._update_pos()

    def _on_accuracy_sample(self, sample: zenoh.Sample):
        """Callback fired by zenoh when the relative frame publishes an accuracy update."""
        payload = sample.payload.to_string()
        if self.synq_session is None:
            raise RuntimeError("synq_session is required to deserialize accuracy")

        new_accuracy = self.synq_session.type_registry.load(payload)
        if not isinstance(new_accuracy, Accuracy):
            raise TypeError(f"{new_accuracy} is {type(new_accuracy)}, not {Accuracy}")
        self._relative_accuracy = new_accuracy
        self._update_pos()

    def _update_pos(self):
        """
        Compute the real (root-frame) position from the parent's world-frame
        state and the mount's local-frame offset/orientation.

        For a root frame (no parent), the mount values are used directly.

        For a relative frame, the rigid-body transform chain applies:
            offset = parent_offset + mount_offset.rotate_by(parent_orientation)
            orientation = parent_orientation * mount_orientation

        This is the standard composition: T_child = T_parent ∘ T_mount
        """
        m_offset = self.mount_offset or Offset()
        m_orient = self.mount_orientation or Orientation()

        # No relative frame — mount values ARE the world values
        if self._relative_offset is None and self._relative_orientation is None:
            self.offset = m_offset or None
            self.orientation = m_orient if m_orient != Orientation() else None
            self.accuracy = self.mount_accuracy
            return

        # Defaults: treat missing relative values as identity
        rel_offset = self._relative_offset or Offset()
        rel_orient = self._relative_orientation or Orientation()

        # Transform mount offset from parent's local frame into the root frame,
        # then add the parent's root-frame position
        self.offset = rel_offset + m_offset.rotate_by(rel_orient)

        # Compose orientations: parent world orientation * mount local orientation
        self.orientation = rel_orient * m_orient

        # Combine parent and mount accuracy in quadrature; propagate whichever is known
        rel_acc = self._relative_accuracy
        m_acc = self.mount_accuracy
        if rel_acc is not None and m_acc is not None:
            self.accuracy = rel_acc + m_acc
        else:
            self.accuracy = rel_acc or m_acc
