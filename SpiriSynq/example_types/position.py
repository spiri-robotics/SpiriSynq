from typing import TypeAlias, Literal, ClassVar, Self
from psygnal import SignalGroupDescriptor
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session
from dataclasses import dataclass
import math

root_frame = Literal["ECEF", "unknown", "error"]

SpacialFrame: TypeAlias = root_frame|str

@dataclass(frozen=True)
class Offset:
    x: float = 0 # X-axis: Equator at Prime Meridian
    y: float = 0 # Y-axis: Equator at 90° East longitude
    z: float = 0 # Z-axis: North Pole direction


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
        cr, sr = math.cos(roll / 2),  math.sin(roll / 2)
        cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
        cy, sy = math.cos(yaw / 2),   math.sin(yaw / 2)

        return cls(
            w=cr * cp * cy + sr * sp * sy,
            x=sr * cp * cy - cr * sp * sy,
            y=cr * sp * cy + sr * cp * sy,
            z=cr * cp * sy - sr * sp * cy,
        )


@dataclass
class Position(SyncableObject):
    """
    A generic object that exists in 3D space relative to another object.
    Adding this to your class lets you figure out its position relative to other
    objects.

    offset and orientation are real position relative to some root frame.
    Mounts are what you actually change. For example if your robot is X above
    the center of the earth, and your root frame it ECEF, you mount the drone X
    above the root.
    """
    relative_to: SpacialFrame = "unknown"
    _relative_offset: Offset|None = None
    _relative_orientation: Orientation|None = None    
    offset: Offset|None = None
    orientation: Orientation|None = None
    #Where the device is relative to its parent
    mount_offset: Offset|None = None
    mount_orientation: Orientation|None = None
    def __post_init__(self):
        return super().__post_init__()
    
    def _update_relative(self):
        """Subscribe to relative_to topic and on update relative offsets"""
        pass
    def _update_pos(self):
        "When either mount or _relative updates, we update the real position"
        pass
