"""Shared fixtures. No GPU, Netdata, Bluetooth, Slack or real clock."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_monitor.config import parse_config  # noqa: E402
from lab_monitor.models import (  # noqa: E402
    GpuReading,
    MachineReading,
    SensorReading,
    SensorState,
    Snapshot,
)

#: Five rooms and three machines; sensors are added per-test.
BASE_CONFIG = {
    "site": {"name": "BioE 3201"},
    "rooms": [
        {"id": "a", "name": "BioE 3201A"},
        {"id": "b", "name": "BioE 3201B"},
        {"id": "c", "name": "BioE 3201C"},
        {"id": "d", "name": "BioE 3201D"},
        {"id": "foyer", "name": "Foyer"},
    ],
    "machines": [
        {
            "id": "deepthought",
            "name": "DeepThought",
            "room": "foyer",
            "netdata_hostname": "DeepThought",
            "parent": True,
        },
        {"id": "gpu2", "name": "gpu2", "room": "a", "netdata_hostname": "gpu2"},
        {"id": "gpu3", "name": "gpu3", "room": "d", "netdata_hostname": "gpu3"},
    ],
    "thresholds": {
        "room_temperature": {
            "high": 82.0,
            "unit": "F",
            "trigger_after_seconds": 600,
            "recovery_margin": 2.0,
        },
        "gpu_temperature": {
            "high": 80.0,
            "unit": "C",
            "trigger_after_seconds": 120,
            "recovery_margin": 3.0,
        },
    },
    "availability": {
        "machine_timeout_seconds": 180,
        "sensor_timeout_seconds": 600,
    },
    "display": {"room_order": ["a", "b", "c", "d", "foyer"]},
    "state": {"path": "/tmp/lab_monitor_test_state.json"},
}


def make_config(overrides=None, env=None, **kwargs):
    """Build a validated Config from :data:`BASE_CONFIG` plus overrides."""
    import copy

    raw = copy.deepcopy(BASE_CONFIG)
    for key, value in (overrides or {}).items():
        raw[key] = value
    raw.update(kwargs)
    return parse_config(raw, env=env or {})


@pytest.fixture
def config():
    """Three machines, five rooms, no sensors."""
    return make_config()


@pytest.fixture
def config_with_sensors():
    """The same lab with one sensor in each of BioE 3201A, B and C."""
    return make_config(
        sensors=[
            {"id": "3201a", "name": "BioE 3201A sensor", "room": "a", "address": "AA:00:00:00:00:01"},
            {"id": "3201b", "name": "BioE 3201B sensor", "room": "b", "address": "AA:00:00:00:00:02"},
            {"id": "3201c", "name": "BioE 3201C sensor", "room": "c", "address": "AA:00:00:00:00:03"},
        ]
    )


# -- snapshot builders ----------------------------------------------------


def gpu(index="0", temperature_c=60.0, **kwargs):
    """A GPU reading with defaults; pass ``None`` to drop a metric."""
    values = {
        "utilization_pct": 40.0,
        "fan_speed_pct": 45.0,
        "power_w": 180.0,
        "vram_used_bytes": 8.0 * 1024 ** 3,
        "vram_total_bytes": 24.0 * 1024 ** 3,
    }
    values.update(kwargs)
    return GpuReading(index=index, temperature_c=temperature_c, **values)


def machine(machine_id, available=True, gpus=(), last_seen=1000.0, error=None):
    """A machine reading."""
    return MachineReading(
        machine_id=machine_id,
        available=available,
        last_seen=last_seen if available else None,
        gpus=tuple(gpus),
        error=error,
    )


def sensor_ok(sensor_id, temperature_c, humidity_pct=40.0, last_seen=1000.0):
    """A healthy sensor reading."""
    return SensorReading(
        sensor_id=sensor_id,
        state=SensorState.OK,
        temperature_c=temperature_c,
        humidity_pct=humidity_pct,
        last_seen=last_seen,
    )


def sensor_state(sensor_id, state, last_seen=None):
    """A sensor reading in a non-OK state (values absent by design)."""
    return SensorReading(sensor_id=sensor_id, state=state, last_seen=last_seen)


def snapshot(now, machines=(), sensors=(), bluetooth_error=None):
    """Assemble a Snapshot at a given time."""
    return Snapshot(
        taken_at=now,
        machines=tuple(machines),
        sensors=tuple(sensors),
        bluetooth_error=bluetooth_error,
    )


def f_to_c(fahrenheit):
    """So temperature tests can be written in Fahrenheit."""
    return (fahrenheit - 32.0) * 5.0 / 9.0


@pytest.fixture
def all_available(config):
    """A builder for "everything is fine" snapshots at time ``now``."""

    def build(now, **kwargs):
        return snapshot(
            now,
            machines=[
                machine("deepthought", gpus=[gpu("0", 55.0)]),
                machine("gpu2", gpus=[gpu("0", 60.0)]),
                machine("gpu3", gpus=[gpu("0", 58.0)]),
            ],
            **kwargs,
        )

    return build
