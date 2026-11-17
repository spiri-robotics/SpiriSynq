"""`@on_change.owner`, `@on_change.mirror`, and signals on `events`."""

from dataclasses import dataclass

import pytest

from SpiriSynq import on_change
from SpiriSynq.syncable_objects import SyncableObject

from .conftest import wait_for


@dataclass
class Battery(SyncableObject):
    synq_type_id = "1a2b3c4d-0000-4000-8000-000000000002:1"
    current_a: float = 0.0
    peak_current_a: float = 0.0
    smoothed_current_a: float = 0.0
    charge_mah: float = 0.0
    capacity_mah: float = 1.0
    percent: float = 0.0
    limit: int = 100

    @on_change.owner("current_a")
    def track_peak(self, value: float) -> None:
        if value > self.peak_current_a:
            self.peak_current_a = value

    @on_change.mirror("current_a")
    def smooth_current(self, value: float) -> None:
        self.smoothed_current_a = 0.5 * self.smoothed_current_a + 0.5 * value

    @on_change.owner("charge_mah", "capacity_mah")
    def update_percent(self, value: float, field: str) -> None:
        self.percent = 100 * self.charge_mah / self.capacity_mah

    @on_change.owner("limit")
    def clamp(self, value: int) -> None:
        self.limit = max(50, value)  # sets its own field: doesn't run again


def test_owner_callbacks_run_however_a_field_changes(other):
    battery = Battery("battery", synq_authoritive=True)
    battery.current_a = 31.0
    assert battery.peak_current_a == 31.0
    battery.current_a = 12.0
    assert battery.peak_current_a == 31.0
    battery.charge_mah = 0.5
    assert battery.percent == 50.0
    battery.limit = 10
    assert battery.limit == 50
    mirror = Battery.from_topic("host/battery", session=other)
    mirror.current_a = 40.0  # a write from a mirror
    wait_for(lambda: battery.peak_current_a == 40.0)
    wait_for(lambda: mirror.peak_current_a == 40.0)


def test_mirror_callbacks_change_only_that_mirror(other):
    battery = Battery("battery", synq_authoritive=True)
    mirror = Battery.from_topic("host/battery", session=other)
    battery.current_a = 10.0
    wait_for(lambda: mirror.smoothed_current_a == 5.0)
    assert battery.smoothed_current_a == 0.0
    battery.peak_current_a = 0.0
    battery.smoothed_current_a = 1.0  # the owner's value replaces the mirror's
    wait_for(lambda: mirror.smoothed_current_a == 1.0)


def test_signals_on_events(other):
    battery = Battery("battery", synq_authoritive=True)
    mirror = Battery.from_topic("host/battery", session=other)
    seen = []
    mirror.events.limit.connect(seen.append)
    battery.limit = 70
    wait_for(lambda: seen == [70])


def test_a_callback_needs_a_field():
    with pytest.raises(TypeError, match="at least one field"):
        on_change.owner()(lambda self, value: None)
    assert Battery.track_peak.fields == ("current_a",)


def test_a_callback_is_still_a_method():
    battery = Battery("battery", synq_authoritive=True)
    battery.track_peak(99.0)
    assert battery.peak_current_a == 99.0
