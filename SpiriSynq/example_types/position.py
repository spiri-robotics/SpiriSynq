from typing import TypeAlias, Literal, ClassVar, Self, get_args
from psygnal import SignalGroupDescriptor
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session
from dataclasses import dataclass, field
import math
import zenoh


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


def fuse_positions(*positions: Position) -> tuple[Offset, Accuracy]:
    """Fuse N independent position observations into a single best estimate.

    Uses inverse-variance weighting when horizontal/vertical accuracies are
    known, falling back to a simple mean otherwise. More observations with
    known accuracy produce a tighter result.

    Returns (offset, accuracy) where either component may have None fields
    if no accuracy information was available for that axis.
    """
    if not positions:
        raise ValueError("At least one position is required")

    offsets = [p.offset or Offset() for p in positions]

    # Pairs of (offset, h_accuracy) for positions that have horizontal accuracy
    h_known = [(off, p.accuracy.horizontal)
               for off, p in zip(offsets, positions)
               if p.accuracy is not None and p.accuracy.horizontal is not None]

    # Pairs of (offset, v_accuracy) for positions that have vertical accuracy
    v_known = [(off, p.accuracy.vertical)
               for off, p in zip(offsets, positions)
               if p.accuracy is not None and p.accuracy.vertical is not None]

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

