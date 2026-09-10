"""Configuration loading and validation.

Two sources kept apart: a TOML file for the stable facts (rooms, machines,
sensors, thresholds, timing, display order), and environment variables for
everything deployment-specific (static IPs, Slack tokens, Netdata URLs).

Validation is strict and errors name the offending key.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from .models import Machine, Room, Sensor, Threshold

DEFAULT_CONFIG_PATH = "/etc/bvl-automations/lab_monitor.toml"
DEFAULT_STATE_PATH = "/var/lib/bvl-automations/lab_monitor_state.json"
DEFAULT_NETDATA_URL = "http://127.0.0.1:19999"

#: Known threshold sections; anything else is rejected as a typo.
THRESHOLD_KEYS = ("room_temperature", "gpu_temperature")


class ConfigError(Exception):
    """Raised when the configuration file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class Availability:
    """How long silence is tolerated before something counts as gone."""

    machine_timeout_seconds: int = 180
    sensor_timeout_seconds: int = 600
    alert_on_machine_unavailable: bool = True
    alert_on_sensor_unavailable: bool = True
    trigger_after_seconds: int = 0
    recover_after_seconds: int = 0


@dataclass(frozen=True)
class Display:
    """Presentation-only settings for the Slack dashboard."""

    room_order: tuple[str, ...] = ()
    temperature_unit: str = "F"
    gpu_temperature_unit: str = "C"
    time_format: str = "%I:%M %p"


@dataclass(frozen=True)
class NetdataSettings:
    """Where Netdata is and how we push room readings back into it."""

    url: str = DEFAULT_NETDATA_URL
    dashboard_url: str | None = None
    timeout_seconds: float = 5.0
    statsd_enabled: bool = True
    statsd_host: str = "127.0.0.1"
    statsd_port: int = 8125
    statsd_prefix: str = "labmonitor"


@dataclass(frozen=True)
class SlackSettings:
    """Slack credentials, resolved from the environment."""

    bot_token: str | None = None
    app_token: str | None = None
    channel_id: str | None = None

    @property
    def configured(self) -> bool:
        """True when we have everything needed to run the Socket Mode app."""
        return bool(self.bot_token and self.app_token)


@dataclass(frozen=True)
class Config:
    """The validated whole. Immutable; reload by constructing a new one."""

    site_name: str
    rooms: tuple[Room, ...]
    machines: tuple[Machine, ...]
    sensors: tuple[Sensor, ...]
    thresholds: dict
    availability: Availability
    display: Display
    netdata: NetdataSettings
    slack: SlackSettings
    poll_interval_seconds: int = 30
    state_path: str = DEFAULT_STATE_PATH
    source_path: str | None = None
    warnings: tuple[str, ...] = ()

    # -- lookups ----------------------------------------------------------

    def room(self, room_id):
        """Return the room with this id, or ``None``."""
        for room in self.rooms:
            if room.id == room_id:
                return room
        return None

    def machine(self, machine_id):
        """Return the machine with this id, or ``None``."""
        for machine in self.machines:
            if machine.id == machine_id:
                return machine
        return None

    def sensor(self, sensor_id):
        """Return the sensor with this id, or ``None``."""
        for sensor in self.sensors:
            if sensor.id == sensor_id:
                return sensor
        return None

    def ordered_rooms(self):
        """Rooms in display order; anything ``room_order`` omits is appended."""
        by_id = {room.id: room for room in self.rooms}
        ordered = [by_id[rid] for rid in self.display.room_order if rid in by_id]
        listed = {room.id for room in ordered}
        ordered.extend(room for room in self.rooms if room.id not in listed)
        return tuple(ordered)

    def machines_in(self, room_id):
        """Machines assigned to a room, in declaration order."""
        return tuple(m for m in self.machines if m.room == room_id)

    def sensors_in(self, room_id):
        """Sensors assigned to a room, in declaration order."""
        return tuple(s for s in self.sensors if s.room == room_id)

    def threshold(self, name):
        """Return a named threshold, or ``None`` if it is not configured."""
        return self.thresholds.get(name)


# -- loading ---------------------------------------------------------------


def load_config(path=None, env=None):
    """Read, validate and return the configuration.

    ``path`` defaults to ``$LAB_MONITOR_CONFIG``, then :data:`DEFAULT_CONFIG_PATH`.
    """
    env = os.environ if env is None else env
    path = path or env.get("LAB_MONITOR_CONFIG") or DEFAULT_CONFIG_PATH

    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(
            "config file not found: %s (set LAB_MONITOR_CONFIG or create it "
            "from config.example.toml)" % path
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("could not parse %s: %s" % (path, exc)) from None
    except OSError as exc:
        raise ConfigError("could not read %s: %s" % (path, exc)) from None

    return parse_config(raw, env=env, source_path=path)


def parse_config(raw, env=None, source_path=None):
    """Validate an already-parsed TOML mapping."""
    env = os.environ if env is None else env
    if not isinstance(raw, dict):
        raise ConfigError("top level of the config must be a table")

    warnings = []

    site = _table(raw, "site", default={})
    site_name = site.get("name") or "Lab"
    if not isinstance(site_name, str):
        raise ConfigError("site.name must be a string")

    rooms = _parse_rooms(raw)
    room_ids = {room.id for room in rooms}
    machines, machine_warnings = _parse_machines(raw, room_ids)
    warnings.extend(machine_warnings)
    sensors, sensor_warnings = _parse_sensors(raw, room_ids, env)
    warnings.extend(sensor_warnings)

    thresholds = _parse_thresholds(raw)
    availability = _parse_availability(raw)
    display = _parse_display(raw, room_ids)
    netdata = _parse_netdata(raw, env)
    slack = _parse_slack(env)

    runtime = _table(raw, "polling", default={})
    poll_interval = _positive_int(runtime, "polling", "interval_seconds", 30)

    state = _table(raw, "state", default={})
    state_path = state.get("path", DEFAULT_STATE_PATH)
    if not isinstance(state_path, str) or not state_path:
        raise ConfigError("state.path must be a non-empty string")

    return Config(
        site_name=site_name,
        rooms=rooms,
        machines=machines,
        sensors=sensors,
        thresholds=thresholds,
        availability=availability,
        display=display,
        netdata=netdata,
        slack=slack,
        poll_interval_seconds=poll_interval,
        state_path=state_path,
        source_path=source_path,
        warnings=tuple(warnings),
    )


# -- section parsers -------------------------------------------------------


def _parse_rooms(raw):
    entries = _array_of_tables(raw, "rooms")
    if not entries:
        raise ConfigError("at least one [[rooms]] entry is required")

    rooms = []
    seen = set()
    for index, entry in enumerate(entries):
        rid = _require_str(entry, "rooms[%d]" % index, "id")
        if rid in seen:
            raise ConfigError("duplicate room id %r" % rid)
        seen.add(rid)
        name = entry.get("name", rid)
        if not isinstance(name, str):
            raise ConfigError("rooms[%d].name must be a string" % index)
        rooms.append(Room(id=rid, name=name))
    return tuple(rooms)


def _parse_machines(raw, room_ids):
    entries = _array_of_tables(raw, "machines")
    if not entries:
        raise ConfigError("at least one [[machines]] entry is required")

    machines = []
    warnings = []
    seen = set()
    parents = []
    for index, entry in enumerate(entries):
        where = "machines[%d]" % index
        mid = _require_str(entry, where, "id")
        if mid in seen:
            raise ConfigError("duplicate machine id %r" % mid)
        seen.add(mid)

        room = _require_str(entry, where, "room")
        if room not in room_ids:
            raise ConfigError(
                "%s.room = %r does not match any [[rooms]] id" % (where, room)
            )

        name = entry.get("name", mid)
        if not isinstance(name, str):
            raise ConfigError("%s.name must be a string" % where)

        hostname = entry.get("netdata_hostname", mid)
        if not isinstance(hostname, str) or not hostname:
            raise ConfigError("%s.netdata_hostname must be a non-empty string" % where)

        is_parent = _bool(entry, where, "parent", False)
        if is_parent:
            parents.append(mid)

        machines.append(
            Machine(
                id=mid,
                name=name,
                room=room,
                netdata_hostname=hostname,
                parent=is_parent,
            )
        )

    if len(parents) > 1:
        raise ConfigError(
            "only one machine may set parent = true, found: %s" % ", ".join(parents)
        )
    if not parents:
        warnings.append(
            "no machine sets parent = true; LabMonitor still queries "
            "NETDATA_URL, but the topology does not say which host that is"
        )
    return tuple(machines), warnings


def _parse_sensors(raw, room_ids, env):
    entries = _array_of_tables(raw, "sensors")
    sensors = []
    warnings = []
    seen = set()
    for index, entry in enumerate(entries):
        where = "sensors[%d]" % index
        sid = _require_str(entry, where, "id")
        if sid in seen:
            raise ConfigError("duplicate sensor id %r" % sid)
        seen.add(sid)

        room = _require_str(entry, where, "room")
        if room not in room_ids:
            raise ConfigError(
                "%s.room = %r does not match any [[rooms]] id" % (where, room)
            )

        name = entry.get("name", sid)
        if not isinstance(name, str):
            raise ConfigError("%s.name must be a string" % where)

        address = entry.get("address")
        if address is not None and not isinstance(address, str):
            raise ConfigError("%s.address must be a string" % where)

        address_env = entry.get("address_env")
        if address_env is not None and not isinstance(address_env, str):
            raise ConfigError("%s.address_env must be a string" % where)
        if address_env and not address:
            address = env.get(address_env)

        if not address:
            warnings.append(
                "sensor %r has no BLE address yet; it will show as "
                "'not yet seen' until one is configured" % sid
            )

        sensors.append(
            Sensor(
                id=sid,
                name=name,
                room=room,
                address=_normalise_address(address),
                address_env=address_env,
            )
        )
    return tuple(sensors), warnings


def _parse_thresholds(raw):
    table = _table(raw, "thresholds", default={})
    unknown = set(table) - set(THRESHOLD_KEYS)
    if unknown:
        raise ConfigError(
            "unknown threshold section(s): %s (expected one of %s)"
            % (", ".join(sorted(unknown)), ", ".join(THRESHOLD_KEYS))
        )

    thresholds = {}
    for key in THRESHOLD_KEYS:
        entry = table.get(key)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise ConfigError("[thresholds.%s] must be a table" % key)
        where = "thresholds.%s" % key

        if "high" not in entry:
            raise ConfigError("%s.high is required" % where)
        high = _number(entry, where, "high")

        unit = entry.get("unit", "C")
        if not isinstance(unit, str) or unit.upper() not in ("C", "F"):
            raise ConfigError("%s.unit must be \"C\" or \"F\"" % where)

        trigger = _positive_int(entry, where, "trigger_after_seconds", 120)
        recover = _positive_int(entry, where, "recover_after_seconds", trigger)
        margin = _number(entry, where, "recovery_margin", 0.0)
        if margin < 0:
            raise ConfigError("%s.recovery_margin must not be negative" % where)

        thresholds[key] = Threshold(
            high=high,
            unit=unit.upper(),
            trigger_after_seconds=trigger,
            recovery_margin=margin,
            recover_after_seconds=recover,
            enabled=_bool(entry, where, "enabled", True),
        )
    return thresholds


def _parse_availability(raw):
    entry = _table(raw, "availability", default={})
    where = "availability"
    trigger = _positive_int(entry, where, "trigger_after_seconds", 0)
    return Availability(
        machine_timeout_seconds=_positive_int(entry, where, "machine_timeout_seconds", 180),
        sensor_timeout_seconds=_positive_int(entry, where, "sensor_timeout_seconds", 600),
        alert_on_machine_unavailable=_bool(entry, where, "alert_on_machine_unavailable", True),
        alert_on_sensor_unavailable=_bool(entry, where, "alert_on_sensor_unavailable", True),
        trigger_after_seconds=trigger,
        recover_after_seconds=_positive_int(entry, where, "recover_after_seconds", trigger),
    )


def _parse_display(raw, room_ids):
    entry = _table(raw, "display", default={})
    order = entry.get("room_order", [])
    if not isinstance(order, list) or any(not isinstance(x, str) for x in order):
        raise ConfigError("display.room_order must be a list of room ids")
    unknown = [rid for rid in order if rid not in room_ids]
    if unknown:
        raise ConfigError(
            "display.room_order references unknown room id(s): %s" % ", ".join(unknown)
        )

    unit = entry.get("temperature_unit", "F")
    if not isinstance(unit, str) or unit.upper() not in ("C", "F"):
        raise ConfigError("display.temperature_unit must be \"C\" or \"F\"")

    gpu_unit = entry.get("gpu_temperature_unit", "C")
    if not isinstance(gpu_unit, str) or gpu_unit.upper() not in ("C", "F"):
        raise ConfigError("display.gpu_temperature_unit must be \"C\" or \"F\"")

    time_format = entry.get("time_format", "%I:%M %p")
    if not isinstance(time_format, str):
        raise ConfigError("display.time_format must be a string")

    return Display(
        room_order=tuple(order),
        temperature_unit=unit.upper(),
        gpu_temperature_unit=gpu_unit.upper(),
        time_format=time_format,
    )


def _parse_netdata(raw, env):
    entry = _table(raw, "netdata", default={})
    where = "netdata"

    url = env.get("NETDATA_URL") or entry.get("url") or DEFAULT_NETDATA_URL
    if not isinstance(url, str):
        raise ConfigError("netdata.url must be a string")

    dashboard = env.get("NETDATA_DASHBOARD_URL") or entry.get("dashboard_url") or None
    if dashboard is not None and not isinstance(dashboard, str):
        raise ConfigError("netdata.dashboard_url must be a string")

    statsd = entry.get("statsd", {})
    if not isinstance(statsd, dict):
        raise ConfigError("[netdata.statsd] must be a table")

    port = statsd.get("port", 8125)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError("netdata.statsd.port must be a valid port number")

    prefix = statsd.get("prefix", "labmonitor")
    if not isinstance(prefix, str) or not prefix:
        raise ConfigError("netdata.statsd.prefix must be a non-empty string")

    return NetdataSettings(
        url=url.rstrip("/"),
        dashboard_url=dashboard,
        timeout_seconds=float(_number(entry, where, "timeout_seconds", 5.0)),
        statsd_enabled=_bool(statsd, "netdata.statsd", "enabled", True),
        statsd_host=statsd.get("host", "127.0.0.1"),
        statsd_port=port,
        statsd_prefix=prefix,
    )


def _parse_slack(env):
    return SlackSettings(
        bot_token=env.get("LAB_MONITOR_SLACK_BOT_TOKEN") or None,
        app_token=env.get("LAB_MONITOR_SLACK_APP_TOKEN") or None,
        channel_id=env.get("LAB_MONITOR_SLACK_CHANNEL_ID") or None,
    )


# -- validation helpers ----------------------------------------------------


def _table(raw, key, default=None):
    value = raw.get(key, default)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("[%s] must be a table" % key)
    return value


def _array_of_tables(raw, key):
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ConfigError("[[%s]] must be an array of tables" % key)
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError("every [[%s]] entry must be a table" % key)
    return value


def _require_str(entry, where, key):
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError("%s.%s is required and must be a non-empty string" % (where, key))
    return value


def _number(entry, where, key, default=None):
    if key not in entry:
        return default
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s.%s must be a number" % (where, key))
    return float(value)


def _bool(entry, where, key, default):
    """Require a real TOML boolean.

    ``bool()`` would quietly accept ``enabled = "false"`` as true, which is
    exactly the config typo strict validation exists to catch.
    """
    if key not in entry:
        return default
    value = entry[key]
    if not isinstance(value, bool):
        raise ConfigError("%s.%s must be true or false" % (where, key))
    return value


def _positive_int(entry, where, key, default):
    if key not in entry:
        return default
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError("%s.%s must be a non-negative integer" % (where, key))
    return value


def _normalise_address(address):
    """Upper-case BLE MACs so config and adapter agree on one spelling."""
    if not address:
        return None
    return address.strip().upper()
