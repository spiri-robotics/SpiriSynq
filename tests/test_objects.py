"""Syncable objects: owners and mirrors, fields and their settings, the state a mirror fetches."""

import time
from dataclasses import dataclass, field

import pytest
from psygnal.containers import EventedDict, EventedList, EventedSet

from SpiriSynq import _core, qos
from SpiriSynq.syncable_objects import Sample, SubSyncableDataclass, SyncableObject

from .conftest import wait_for


@dataclass
class Gps(SubSyncableDataclass):
    latitude: float = 0.0
    longitude: float = 0.0
    longitude_qos = qos.realtime


@dataclass(frozen=True)
class Cell:
    volts: float = 0.0
    label: str = ""


@dataclass
class Battery(SyncableObject):
    synq_type_id = "2f8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
    synq_skip_sync = {"local"}
    synq_skip_rehydrate = {"image"}

    voltage: float = 0.0
    charging: bool = False
    cells: list[float] = field(default_factory=list)
    gps: Gps | None = None
    cell: Cell = field(default_factory=Cell)
    image: bytes = b""
    local: dict = field(default_factory=dict)
    history: EventedList = field(default_factory=EventedList)
    tags: EventedDict = field(default_factory=EventedDict)
    flags: EventedSet = field(default_factory=EventedSet)
    by_cell: dict[int, float] = field(default_factory=dict)
    voltage_qos = qos.interactive_high
    charging_reliability = qos.reliable


def owner(**fields) -> Battery:
    return Battery("./battery", synq_authoritive=True, **fields)


def test_a_mirror_fetches_the_state_then_follows(other):
    battery = owner(voltage=12.5, cells=[3.1, 3.2], gps=Gps(45.0, -75.0), cell=Cell(3.3, "a"))
    assert battery.synq_absolute_path == "host/battery"
    mirror = Battery.from_topic("host/battery", session=other)
    assert (mirror.voltage, mirror.cells, mirror.gps, mirror.cell) == (
        12.5,
        [3.1, 3.2],
        Gps(45.0, -75.0),
        Cell(3.3, "a"),
    )
    battery.voltage = 11.0
    battery.charging = True
    wait_for(lambda: (mirror.voltage, mirror.charging) == (11.0, True))
    assert mirror.synq_alive
    assert battery.synq_alive


def test_nested_dataclass_fields_sync_one_by_one(other):
    battery = owner(gps=Gps())
    mirror = Battery.from_topic("host/battery", session=other)
    seen = []
    mirror.events.connect(lambda info: seen.append(info))
    battery.gps.latitude = 45.5
    wait_for(lambda: mirror.gps.latitude == 45.5)
    battery.gps = None
    wait_for(lambda: mirror.gps is None)
    battery.gps = Gps(1.0, 2.0)
    wait_for(lambda: mirror.gps == Gps(1.0, 2.0))
    assert seen


def test_a_mirrors_write_reaches_the_owner_and_comes_back(other):
    battery = owner()
    mirror = Battery.from_topic("host/battery", session=other)
    mirror.voltage = 9.0
    assert mirror.voltage == 9.0
    wait_for(lambda: battery.voltage == 9.0)
    mirror.gps = Gps(3.0, 4.0)
    wait_for(lambda: battery.gps == Gps(3.0, 4.0))


def test_containers_are_sent_whole_when_they_change(other):
    battery = owner()
    mirror = Battery.from_topic("host/battery", session=other)
    battery.history.append(1)
    wait_for(lambda: list(mirror.history) == [1])
    battery.tags["a"] = 1
    wait_for(lambda: dict(mirror.tags) == {"a": 1})
    battery.flags.add("x")
    wait_for(lambda: set(mirror.flags) == {"x"})
    battery.by_cell = {1: 3.5}
    wait_for(lambda: mirror.by_cell == {1: 3.5})
    battery.cells = [1.0]
    wait_for(lambda: mirror.cells == [1.0])


def test_skipped_fields_stay_local(other):
    battery = owner(local={"a": 1}, image=b"\x89PNG")
    mirror = Battery.from_topic("host/battery", session=other)
    assert mirror.local == {}
    assert mirror.image == b""  # not in the fetched state
    battery.image = b"new"
    wait_for(lambda: mirror.image == b"new")
    assert "local" not in Battery.synq_schema()["properties"]


def test_closing_the_owner_is_seen_by_mirrors(other):
    battery = owner()
    mirror = Battery.from_topic("host/battery", session=other)
    heard = []
    mirror.synq_signal_alive.connect(heard.append)
    battery.close()
    battery.close()  # twice is fine
    wait_for(lambda: heard == [False])
    assert not mirror.synq_alive
    mirror.close()


def test_from_topic_waits_for_an_owner(other):
    with pytest.raises(TimeoutError):
        Battery.from_topic("host/nobody", session=other, timeout=0.5)
    mirror = Battery.from_topic("host/later", session=other, wait=False)
    assert mirror.voltage == 0.0
    later = Battery("later", synq_authoritive=True, voltage=2.0)
    wait_for(lambda: mirror.voltage == 2.0)
    mirror.synq_rehydrate()
    assert mirror.voltage == 2.0
    mirror.close()
    later.close()
    with pytest.raises(TimeoutError):
        Battery.from_topic("host/later2", session=other, wait=False).synq_rehydrate(timeout=0.3)


def test_topics_are_relative_to_the_base_topic(session):
    assert Battery("./a", synq_authoritive=True).synq_absolute_path == "host/a"
    assert Battery("b", synq_authoritive=True).synq_absolute_path == "host/b"
    assert Battery("c", synq_authoritive=True, synq_base_topic="x").synq_absolute_path == "x/c"
    assert session.resolve_topic("./d") == "host/d"
    assert session.resolve_topic(".") == "host"
    for bad in ["a/../b", "a//b", "a/*", "x/./y"]:
        with pytest.raises(ValueError):
            session.resolve_topic(bad)


def test_an_object_that_doesnt_start_by_itself():
    battery = Battery("late", synq_authoritive=True, synq_auto_start=False)
    assert getattr(battery, "_synq_id", None) is None
    battery.close()  # never started: nothing to do
    battery.sync()
    assert battery.synq_alive


def test_str_shows_the_fields():
    battery = owner(voltage=1.5)
    assert str(battery).startswith("Battery('host/battery', voltage=1.5")

    @dataclass
    class Empty(SyncableObject):
        synq_type_id = "aa8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"

    assert str(Empty("e", synq_authoritive=True)) == "Empty('host/e')"


@dataclass
class Quiet(SyncableObject):
    synq_type_id = "3f8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
    reading: float = 0.0
    reading_ignore_incoming = True
    reading_lazy = True
    setting: int = 0
    setting_ignore_incoming = True


def test_ignored_writes_and_lazy_fields(other):
    quiet = Quiet("quiet", synq_authoritive=True)
    assert not quiet.synq_has_listeners("reading")
    heard = []
    quiet.synq_signal_listeners.connect(lambda path, on: heard.append((path, on)))
    mirror = Quiet.from_topic("host/quiet", session=other)
    wait_for(lambda: quiet.synq_has_listeners("reading"))
    assert quiet.synq_has_listeners()
    wait_for(lambda: ("reading", True) in heard)
    mirror.setting = 5
    time.sleep(0.3)
    assert quiet.setting == 0
    quiet.reading = 1.0
    wait_for(lambda: mirror.reading == 1.0)


def test_field_settings_must_be_presets():
    @dataclass
    class Wrong(SyncableObject):
        synq_type_id = "4f8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
        x: int = 0
        x_qos = "fast"

    with pytest.raises(TypeError, match="x_qos must be a qos preset"):
        Wrong("w", synq_authoritive=True)

    @dataclass
    class Wrong2(SyncableObject):
        synq_type_id = "5f8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
        x: int = 0
        x_reliability = qos.data

    with pytest.raises(TypeError, match="x_reliability must be"):
        Wrong2("w", synq_authoritive=True)
    assert repr(qos.data) == "qos.data"
    assert (repr(qos.reliable), repr(qos.drop)) == ("qos.reliable", "qos.drop")


def test_lazy_needs_ignore_incoming():
    @dataclass
    class Lazy(SyncableObject):
        synq_type_id = "6f8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
        x: int = 0
        x_lazy = True

    with pytest.raises(_core.SynqError, match="must also ignore incoming"):
        Lazy("l", synq_authoritive=True)


def test_values_that_dont_fit_are_bad_data(other):
    battery = owner()
    mirror = Battery.from_topic("host/battery", session=other)
    bad = []
    mirror.synq_signal_bad_data.connect(lambda path, reason: bad.append((path, reason)))
    node = battery.synq_session.node
    node.send_raw(battery._synq_id, "voltage", _core.Sample(b"not a number", "application/yaml"))
    wait_for(lambda: bad)
    assert bad[0][0] == "voltage" and "expected float" in bad[0][1]
    node.send_raw(battery._synq_id, "voltage", _core.Sample(b"\x00", "zenoh/bytes"))
    wait_for(lambda: len(bad) == 2)
    assert "isn't YAML" in bad[1][1]
    node.send_raw(battery._synq_id, "voltage", _core.Sample(b"[unclosed", "application/yaml"))
    wait_for(lambda: len(bad) == 3)
    assert mirror.voltage == 0.0


def test_a_write_into_a_missing_dataclass_is_bad_data(other):
    battery = owner()
    mirror = Battery.from_topic("host/battery", session=other)
    bad = []
    battery.synq_signal_bad_data.connect(lambda path, reason: bad.append(path))
    other.node.set_field(mirror._synq_id, "gps", {"latitude": 1.0, "longitude": 2.0})
    other.node.set_field(mirror._synq_id, "gps/latitude", 5.0)
    wait_for(lambda: battery.gps is not None and battery.gps.latitude == 5.0)
    battery.gps = None
    mirror_bad = []
    mirror.synq_signal_bad_data.connect(lambda path, reason: mirror_bad.append(reason))
    wait_for(lambda: mirror.gps is None)
    # A sub-field arriving while the mirror holds no Gps can't be applied anywhere.
    battery.synq_session.node.send_raw(
        battery._synq_id, "gps/latitude", _core.Sample(b"1.0", "application/yaml")
    )
    wait_for(lambda: mirror_bad)


def test_an_owner_reports_writes_it_cant_apply(other):
    battery = owner()
    mirror = Battery.from_topic("host/battery", session=other)
    bad = []
    battery.synq_signal_bad_data.connect(lambda path, reason: bad.append(reason))
    other.node.send_raw(mirror._synq_id, "gps/latitude", _core.Sample(b"5.0", "application/yaml"))
    wait_for(lambda: bad)
    assert "is None" in bad[0]


@dataclass
class Camera(SyncableObject):
    synq_type_id = "7f8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
    frame: str | None = None
    frame_qos = qos.background
    caption: str = ""

    def frame_before_send(self, frame: str, sample: Sample) -> None:
        sample.send(frame.encode()[::-1], encoding="text/reversed", attachment=b"meta")

    def frame_after_receive(self, sample: Sample) -> str:
        assert sample.attachment == b"meta"
        return sample.payload[::-1].decode()

    def caption_before_send(self, caption: str, sample: Sample) -> None:
        sample.send_value(caption.upper())

    def caption_after_receive(self, sample: Sample) -> str:
        return sample.decode().lower()


def test_fields_with_their_own_encoding(other):
    camera = Camera("camera", synq_authoritive=True, frame="first", caption="Hi")
    mirror = Camera.from_topic("host/camera", session=other)
    assert (mirror.frame, mirror.caption) == ("first", "hi")
    camera.frame = "second"
    wait_for(lambda: mirror.frame == "second")
    mirror.frame = "from the mirror"
    wait_for(lambda: camera.frame == "from the mirror")


def test_a_before_send_must_send_once():
    @dataclass
    class Silent(SyncableObject):
        synq_type_id = "8f8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
        x: int | None = None

        def x_before_send(self, x, sample):
            if x == 2:
                sample.send(b"a")
                sample.send(b"b")

    silent = Silent("s", synq_authoritive=True)
    with pytest.raises(Exception, match="returned without sending"):
        silent.x = 1
    with pytest.raises(Exception, match="once"):
        silent.x = 2
    with pytest.raises(ValueError, match="isn't encoded the usual way"):
        Sample(b"x", "zenoh/bytes").decode()


def test_unsendable_values_raise():
    battery = owner()
    with pytest.raises(Exception, match="can't be sent"):
        battery.local = {}  # skipped: fine
        battery.cells = [object()]


def test_a_class_without_identity_cant_be_created():
    @dataclass
    class Nameless(SyncableObject):
        x: int = 0

    with pytest.raises(TypeError, match="has no type identity"):
        Nameless("n", synq_authoritive=True)


def test_a_schema_the_core_refuses_is_a_type_error():
    @dataclass
    class Clash(SyncableObject):
        synq_type_id = "9f8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
        sr_thing: int = 0

    with pytest.raises(TypeError, match="Clash"):
        Clash("c", synq_authoritive=True)


def test_untyped_mirrors(session, other):
    battery = owner(voltage=4.0, cells=[1.0])
    mirror = other.from_topic_untyped("host/battery")
    assert (mirror.voltage, mirror.cells) == (4.0, [1.0])
    assert type(mirror).__name__ == "Untyped"
    battery.voltage = 5.0
    wait_for(lambda: mirror.voltage == 5.0)
    with pytest.raises(LookupError):
        other.fetch_schema("host/nobody", timeout=0.5)


@dataclass
class Rover(SyncableObject):
    synq_type_id = "af8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
    gps: Gps = field(default_factory=Gps)
    limit: int = 0


def test_a_nested_dataclass_that_cant_be_none(other):
    rover = Rover("rover", synq_authoritive=True)
    mirror = Rover.from_topic("host/rover", session=other)
    rover.gps.longitude = 9.0
    wait_for(lambda: mirror.gps.longitude == 9.0)


def test_the_edges_of_applying_values(other):
    rover = Rover("rover", synq_authoritive=True)
    battery = owner()
    assert battery._synq_get("gps/latitude") is None
    bad = []
    rover.synq_signal_bad_data.connect(lambda path, reason: bad.append(path))
    # A field the core holds a value for that doesn't fit, found after WASM code ran.
    rover.synq_session.node.set_field(rover._synq_id, "limit", "high")
    rover._synq_refresh()
    assert bad == ["limit"]
    # A value nobody could send is never the same as one that arrived.
    object.__setattr__(rover, "limit", object())
    assert not rover._synq_same("limit", 1)
    # A notice for a field this class doesn't know is ignored.
    rover._synq_incoming(type("Notice", (), {"path": "nope", "data": 1, "origin": "owner"})())


@dataclass
class Jpeg(SubSyncableDataclass):
    quality: int = 80
    data: bytes = b""
    data_on_demand = True
    data_qos = qos.background

    def data_before_send(self, data: bytes, sample: Sample) -> None:
        sample.send(data, encoding="image/jpeg")

    def data_after_receive(self, sample: Sample) -> bytes:
        return sample.payload


@dataclass
class Streamer(SyncableObject):
    synq_type_id = "bf8a6c1e-3b7d-4c9a-8e5f-1a2b3c4d5e6f:1"
    exposure: float = 0.01
    jpeg: Jpeg = field(default_factory=Jpeg)
    frame: str = ""
    frame_on_demand = True


def test_on_demand_fields(other):
    schema = Streamer.synq_schema()
    assert "@frame" in schema["properties"]
    assert "@data" in schema["$defs"]["Jpeg"]["properties"]
    assert Streamer.synq_key_of("jpeg/data") == "jpeg/@data"
    streamer = Streamer("streamer", synq_authoritive=True, frame="first")
    streamer.jpeg.data = b"\xff\xd8one"
    mirror = Streamer.from_topic("host/streamer", session=other)
    assert (mirror.frame, mirror.jpeg.data, mirror.jpeg.quality) == ("", b"", 80)
    assert not streamer.synq_has_listeners("frame")
    heard = []
    streamer.synq_signal_listeners.connect(lambda path, on: heard.append((path, on)))
    # Subscribing fetches the last value, then follows.
    assert mirror.synq_subscribe("frame")
    assert mirror.synq_subscribe("jpeg/data")
    wait_for(lambda: (mirror.frame, mirror.jpeg.data) == ("first", b"\xff\xd8one"))
    wait_for(lambda: ("frame", True) in heard)
    streamer.frame = "second"
    streamer.jpeg.data = b"\xff\xd8two"
    wait_for(lambda: (mirror.frame, mirror.jpeg.data) == ("second", b"\xff\xd8two"))
    # A frame isn't part of its dataclass's value: replacing the JPEG settings doesn't send one.
    streamer.jpeg = Jpeg(quality=50, data=b"not sent whole")
    wait_for(lambda: mirror.jpeg.quality == 50)
    assert mirror.jpeg.data == b""
    assert mirror.synq_unsubscribe("frame")
    wait_for(lambda: not streamer.synq_has_listeners("frame"))
    streamer.frame = "third"
    time.sleep(0.2)
    assert mirror.frame == "second"
