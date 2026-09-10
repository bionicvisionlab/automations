"""Domain models: plain data, no I/O.

Adapters translate the outside world into these types; ``status`` and
``alerts`` read nothing else. Temperatures are Celsius throughout; Fahrenheit
exists only at threshold comparison and rendering.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


def c_to_f(celsius):
    """Convert Celsius to Fahrenheit."""
    return celsius * 9.0 / 5.0 + 32.0


def f_to_c(fahrenheit):
    """Convert Fahrenheit to Celsius."""
    return (fahrenheit - 32.0) * 5.0 / 9.0


def convert_from_c(celsius, unit):
    """Convert Celsius into ``unit`` (``"C"`` or ``"F"``)."""
    return c_to_f(celsius) if unit.upper() == "F" else celsius


def convert_to_c(value, unit):
    """Convert a value expressed in ``unit`` into Celsius."""
    return f_to_c(value) if unit.upper() == "F" else value


# -- topology --------------------------------------------------------------


@dataclass(frozen=True)
class Room:
    """A physical space, e.g. "BioE 3201A"."""

    id: str
    name: str


@dataclass(frozen=True)
class Machine:
    """A GPU workstation.

    ``netdata_hostname`` is what the machine reports to the Parent and what we
    match on. ``address_env`` names an environment variable holding its static
    IP; the address itself never lives in the repo.
    """

    id: str
    name: str
    room: str
    netdata_hostname: str
    address_env: str | None = None
    parent: bool = False


@dataclass(frozen=True)
class Sensor:
    """A Govee BLE sensor assigned to a room.

    ``address`` is the BLE MAC, and may be ``None`` before discovery.
    """

    id: str
    name: str
    room: str
    address: str | None = None
    address_env: str | None = None

    @property
    def has_address(self) -> bool:
        """True once we know which physical device this entry refers to."""
        return bool(self.address)


@dataclass(frozen=True)
class Threshold:
    """An upper bound in ``unit``, with hysteresis and a debounce window.

    Trips after the value stays above ``high`` for ``trigger_after_seconds``;
    recovers after it stays at or below ``high - recovery_margin`` for
    ``recover_after_seconds``.
    """

    high: float
    unit: str = "C"
    trigger_after_seconds: int = 120
    recovery_margin: float = 0.0
    recover_after_seconds: int = 0
    enabled: bool = True

    def limit(self, alerting: bool) -> float:
        """Comparison limit, lowered by ``recovery_margin`` while alerting."""
        return self.high - self.recovery_margin if alerting else self.high

    def is_abnormal_c(self, celsius: float, alerting: bool) -> bool:
        """Whether a Celsius reading is out of range right now."""
        return convert_from_c(celsius, self.unit) > self.limit(alerting)


# -- current state ---------------------------------------------------------


class SensorState(str, enum.Enum):
    """Lifecycle of a configured room sensor.

    ``NEVER_SEEN`` has never reported (a setup detail, stays quiet).
    ``PENDING`` was established before a restart but has not reported yet in
    this process; it neither alerts nor recovers, and becomes ``STALE`` if the
    grace period runs out. ``STALE`` reported and then stopped: a real fault.
    """

    NEVER_SEEN = "never_seen"
    PENDING = "pending"
    OK = "ok"
    STALE = "stale"


@dataclass(frozen=True)
class GpuReading:
    """One GPU's current metrics. Every metric is optional; missing is never zero."""

    index: str
    product_name: str | None = None
    temperature_c: float | None = None
    utilization_pct: float | None = None
    fan_speed_pct: float | None = None
    power_w: float | None = None
    vram_used_bytes: float | None = None
    vram_total_bytes: float | None = None

    @property
    def label(self) -> str:
        """Short display label, e.g. ``GPU0``."""
        return "GPU%s" % self.index


@dataclass(frozen=True)
class MachineReading:
    """A workstation's current state as seen through the Netdata Parent."""

    machine_id: str
    available: bool
    last_seen: float | None = None
    gpus: tuple[GpuReading, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class SensorReading:
    """A room sensor's current state.

    Value fields are ``None`` unless ``state`` is ``OK``: a stale sensor never
    presents its last reading as though it were current.
    """

    sensor_id: str
    state: SensorState
    temperature_c: float | None = None
    humidity_pct: float | None = None
    battery_pct: float | None = None
    last_seen: float | None = None


@dataclass(frozen=True)
class Snapshot:
    """Everything LabMonitor currently believes about the lab.

    The single input to both the renderer and the alert engine.
    """

    taken_at: float
    machines: tuple[MachineReading, ...] = ()
    sensors: tuple[SensorReading, ...] = ()
    bluetooth_error: str | None = None

    def machine(self, machine_id):
        """Look up a machine reading by id."""
        for reading in self.machines:
            if reading.machine_id == machine_id:
                return reading
        return None

    def sensor(self, sensor_id):
        """Look up a sensor reading by id."""
        for reading in self.sensors:
            if reading.sensor_id == sensor_id:
                return reading
        return None


# -- alert transitions -----------------------------------------------------


class TransitionKind(str, enum.Enum):
    """Which way a condition crossed."""

    ALERT = "alert"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class Transition:
    """A state change worth telling Slack about.

    ``headline`` is the one line placed above the full dashboard.
    """

    key: str
    kind: TransitionKind
    headline: str


@dataclass
class ConditionState:
    """Debounce bookkeeping for one condition.

    ``state`` is committed (and already reported); ``pending`` is a candidate
    change that has not yet lasted long enough to count.
    """

    state: str = "normal"
    pending: str | None = None
    pending_since: float | None = None

    def to_json(self):
        """Serialise for the state file."""
        return {
            "state": self.state,
            "pending": self.pending,
            "pending_since": self.pending_since,
        }

    @classmethod
    def from_json(cls, raw):
        """Rebuild from the state file, tolerating junk."""
        if not isinstance(raw, dict):
            return cls()
        state = raw.get("state")
        pending = raw.get("pending")
        since = raw.get("pending_since")
        return cls(
            state="alert" if state == "alert" else "normal",
            pending=pending if pending in ("alert", "normal") else None,
            pending_since=float(since) if isinstance(since, (int, float)) else None,
        )
