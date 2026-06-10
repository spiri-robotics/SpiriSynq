from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session
from dataclasses import dataclass, field
from PIL import Image
from typing import Literal, TypeAlias, ClassVar, Self
import io
from psygnal import SignalGroupDescriptor


@dataclass
class Camera(SyncableObject):
    max_resolution: tuple[int, int] = (-1, -1)
    max_supported_resolution: tuple[int,int]|None = None
    max_framerate: int = 30
    raw_image: Image.Image | None = None
    sensor_width_mm: float|None = None
    sensor_height_mm: float|None = None
    sensor_focal_length_mm: float|None = None
    sensor_max_supported_focal_length_mm: float|None = None
    sensor_min_supported_focal_length_mm: float|None = None


@dataclass
class MjpegCamera(Camera):
    jpeg_quality: int = 90  # Range from 0 to 100
    jpeg_optimize: bool = True
    jpeg_image: bytes | None = None  # Raw JPEG bytes

    _auto_convert_to_raw: bool = True
    skip_rehydrate = {"jpeg_image"}
    reserved_names = {"raw_image"}  # Don't send raw image over the wire.

    def __post_init__(self):
        self.events.jpeg_image.connect(self.convert_to_raw)
        return super().__post_init__()

    def convert_to_raw(self, new_value: bytes | None):
        if not self._auto_convert_to_raw or new_value is None:
            return
        self.raw_image = Image.open(io.BytesIO(new_value))

BatteryFault = Literal[
    "deep_discharge",
    "spikes",
    "cell_fail",
    "over_current",
    "over_temperature",
    "under_temperature",
    "incompatible_voltage",
    "incompatible_firmware",
    "incompatible_cells_legacy",
]

_FAULT_BITS: list[BatteryFault] = [
    "deep_discharge",
    "spikes",
    "cell_fail",
    "over_current",
    "over_temperature",
    "under_temperature",
    "incompatible_voltage",
    "incompatible_firmware",
    "incompatible_cells_legacy",
]

def parse_fault_bitmask(bitmask: int) -> list[BatteryFault]:
    return [fault for i, fault in enumerate(_FAULT_BITS) if bitmask & (1 << i)]


# MAV_BATTERY_FUNCTION
BatteryFunction = Literal[
    "unknown",      # 0 - Not specified/unknown
    "all",          # 1 - Autopilot, propulsion, payload, all
    "propulsion",   # 2 - Propulsion only
    "avionics",     # 3 - Avionics only
    "payload",      # 4 - Payload only
]

# MAV_BATTERY_TYPE (chemistry)
BatteryType = Literal[
    "unknown",  # 0
    "lipo",     # 1
    "life",     # 2
    "lion",     # 3
    "nimh",     # 4
]

# MAV_BATTERY_CHARGE_STATE
BatteryChargeState = Literal[
    "undefined",        # 0 - Not supported
    "ok",               # 1 - Healthy, not charging
    "low",              # 2 - Low, return or abort
    "critical",         # 3 - Critical, return or abort immediately
    "emergency",        # 4 - Failsafe active
    "failed",           # 5 - Battery failed, shut down system
    "unhealthy",        # 6 - Inspect before next use
    "charging",         # 7 - Charging
]

# MAV_BATTERY_MODE
BatteryMode = Literal[
    "unknown",          # 0 - Not supported or normal mode
    "auto_discharging", # 1 - Auto discharging to storage level
    "hot_swap",         # 2 - Allows auto-swap when depleted
]

@dataclass
class MavBattery(SyncableObject):
    id: int = 0
    warn_non_evented = False #All our lists should update as singletons
    battery_function: BatteryFunction = "unknown"
    type: BatteryType = "unknown"
    voltages: list[int | None] = field(default_factory=list)
    voltages_ext: list[int | None] = field(default_factory=list)
    current_battery: int | None = None
    current_consumed: int | None = None
    energy_consumed: int | None = None
    temperature: int | None = None
    battery_remaining: int | None = None
    time_remaining: int | None = None
    charge_state: BatteryChargeState = "undefined"
    mode: BatteryMode = "unknown"
    fault_bitmask: int = 0

    def faults(self) -> list[BatteryFault]:
        return parse_fault_bitmask(self.fault_bitmask)
    
# --- Shared GPS types ---

GpsFixType = Literal[
    "no_gps",       # 0 - No GPS connected
    "no_fix",       # 1 - No position information
    "2d_fix",       # 2 - 2D position
    "3d_fix",       # 3 - 3D position
    "dgps",         # 4 - DGPS/SBAS aided 3D position
    "rtk_float",    # 5 - RTK float, 3D position
    "rtk_fixed",    # 6 - RTK fixed, 3D position
    "static",       # 7 - Static, typically used for base stations
    "ppp",          # 8 - PPP, 3D position
]


# --- Generic GPS ---

@dataclass
class GPS(SyncableObject):
    """Generic GPS data, common across NMEA, MAVLink, UBlox, etc."""
    fix_type: GpsFixType = "no_gps"
    latitude: int | None = None             # WGS84 latitude in degE7
    longitude: int | None = None            # WGS84 longitude in degE7
    altitude: int | None = None             # Altitude MSL in mm
    hdop: int | None = None                 # Horizontal dilution * 100
    vdop: int | None = None                 # Vertical dilution * 100
    velocity: int | None = None             # Ground speed in cm/s
    course_over_ground: int | None = None   # Course over ground in cdeg (0–36000)
    satellites_visible: int | None = None
    heading: int | None = None              # Vehicle heading in degE5

    @property
    def has_fix(self) -> bool:
        return self.fix_type not in ("no_gps", "no_fix")


@dataclass
class MavGPS(GPS):
    """MAVLink GPS_RAW_INT extensions on top of generic GPS, as well as IMU data so we can calculate
    heading properly.
    """
    altitude_ellipsoid: int | None = None       # Altitude above WGS84 ellipsoid in mm
    horizontal_uncertainty: int | None = None   # mm
    vertical_uncertainty: int | None = None     # mm
    velocity_uncertainty: int | None = None     # mm/s
    heading_uncertainty: int | None = None      # degE5


@dataclass
class MavFcu(SyncableObject):
    """
    Flight control unit running on mavlink
    """
    mavlink_in: str = ""
    mavlink_out: str = ""


@dataclass
class Drone(SyncableObject):
    pass
    # camera: Camera|None
    # fcu: MavFcu
    # gps: GPS
    # battery: MavBattery