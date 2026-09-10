"""Snapshot assembly and the compact text dashboard.

The only rendering in LabMonitor: ``/labstatus`` and every transition
notification both go through :func:`render_dashboard`, so an alert always
shows the whole lab. Abnormal values come from the
:class:`~lab_monitor.alerts.Assessment` rather than being re-derived here.
"""

from __future__ import annotations

import datetime

from .models import SensorState, Snapshot, convert_from_c
from .netdata import collect_machine

#: Minimum dashboard width, so a quiet lab still looks like a dashboard.
MIN_WIDTH = 44

#: Shown in place of a value we do not currently have.
MISSING = "--"

_FLAG = "(!)"
_GIB = 1024.0 ** 3


# -- snapshot assembly -----------------------------------------------------


def build_snapshot(config, netdata_client, sensor_store, now):
    """Gather current state for every configured machine and sensor."""
    machines = tuple(
        collect_machine(
            netdata_client,
            machine,
            now,
            config.availability.machine_timeout_seconds,
        )
        for machine in config.machines
    )
    sensors = tuple(
        sensor_store.reading(sensor, now, config.availability.sensor_timeout_seconds)
        for sensor in config.sensors
    )
    return Snapshot(
        taken_at=now,
        machines=machines,
        sensors=sensors,
        bluetooth_error=sensor_store.bluetooth_error,
    )


# -- dashboard -------------------------------------------------------------


def render_dashboard(config, snapshot, assessment):
    """Render the monospaced dashboard. Plain text; see :func:`render_message`."""
    body = []
    body.extend(_environment_section(config, snapshot, assessment))
    body.append("")
    body.extend(_compute_section(config, snapshot, assessment))

    width = max([MIN_WIDTH] + [len(line) for line in body])
    header = _header(config, snapshot, width)
    return "\n".join(header + body).rstrip()


def _header(config, snapshot, width):
    clock = _format_clock(snapshot.taken_at, config.display.time_format)
    left = config.site_name
    gap = max(1, width - len(left) - len(clock))
    return [left + " " * gap + clock, "─" * width, ""]


def _format_clock(timestamp, time_format):
    """Format the snapshot time, trimming the leading zero (``%-I`` is not portable)."""
    stamp = datetime.datetime.fromtimestamp(timestamp)
    text = stamp.strftime(time_format)
    if text.startswith("0"):
        text = text[1:]
    return text


# -- environment -----------------------------------------------------------


def _environment_section(config, snapshot, assessment):
    lines = ["ENVIRONMENT"]
    if not config.sensors:
        lines.append("  No room sensors configured")
        if snapshot.bluetooth_error:
            lines.append("  Bluetooth: %s" % snapshot.bluetooth_error)
        return lines

    unit = config.display.temperature_unit
    rows = []
    for room in config.ordered_rooms():
        sensors = config.sensors_in(room.id)
        for sensor in sensors:
            label = room.name if len(sensors) == 1 else "%s %s" % (room.name, sensor.name)
            rows.append(_environment_row(label, sensor, snapshot, assessment, unit))

    lines.extend(_grid(rows, ("<", ">", "<", ">", "<"), attached={2}))
    if snapshot.bluetooth_error:
        lines.append("  Bluetooth: %s" % snapshot.bluetooth_error)
    return lines


def _environment_row(label, sensor, snapshot, assessment, unit):
    reading = snapshot.sensor(sensor.id)
    if reading is None or reading.state is SensorState.NEVER_SEEN:
        return [label, MISSING, "", "", "not yet seen"]

    if reading.state is SensorState.PENDING:
        return [label, MISSING, "", "", "awaiting reading"]

    if reading.state is SensorState.STALE:
        note = "unavailable"
        if assessment.sensor_unavailable(sensor.id):
            note += " " + _FLAG
        return [label, MISSING, "", "", note]

    temperature = _temperature(reading.temperature_c, unit)
    flag = _FLAG if assessment.room_temperature_abnormal(sensor.room) else ""
    humidity = "" if reading.humidity_pct is None else "%d%%" % round(reading.humidity_pct)
    return [label, temperature, flag, humidity, ""]


# -- compute ---------------------------------------------------------------


def _compute_section(config, snapshot, assessment):
    """Render machines grouped by room.

    GPU rows share one column grid; per-machine notes are laid out separately
    against the same name width so a long note cannot widen the grid.
    """
    entries = []
    for room in config.ordered_rooms():
        machines = config.machines_in(room.id)
        if not machines:
            continue
        entries.append(("room", room.name))
        for machine in machines:
            entries.extend(_machine_entries(config, machine, snapshot, assessment))

    if not entries:
        return ["COMPUTE", "  No machines configured"]

    gpu_rows = [entry[1] for entry in entries if entry[0] == "gpu"]
    widths = _widths(gpu_rows)
    name_width = max(
        [len(entry[1][0]) for entry in entries if entry[0] == "gpu"]
        + [len(entry[1]) for entry in entries if entry[0] == "note"]
        + [0]
    )
    if widths:
        widths[0] = name_width

    lines = ["COMPUTE"]
    first_room = True
    for kind, *payload in entries:
        if kind == "room":
            if not first_room:
                lines.append("")
            first_room = False
            lines.append(payload[0])
        elif kind == "gpu":
            lines.append(
                "  "
                + _row(
                    payload[0],
                    widths,
                    ("<", "<", ">", "<", ">", "<", ">", ">"),
                    attached={3},
                )
            )
        else:
            name, note = payload
            lines.append("  " + name.ljust(name_width) + "  " + note)
    return lines


def _machine_entries(config, machine, snapshot, assessment):
    reading = snapshot.machine(machine.id)
    if reading is None or not reading.available:
        note = "unavailable"
        if assessment.machine_unavailable(machine.id):
            note += " " + _FLAG
        return [("note", machine.name, note)]

    if not reading.gpus:
        return [("note", machine.name, "no GPUs reported")]

    unit = config.display.gpu_temperature_unit
    entries = []
    for position, gpu in enumerate(reading.gpus):
        entries.append(
            (
                "gpu",
                [
                    machine.name if position == 0 else "",
                    gpu.label,
                    _temperature(gpu.temperature_c, unit, decimals=0),
                    _FLAG if assessment.gpu_temperature_abnormal(machine.id, gpu.index) else "",
                    _percent(gpu.utilization_pct),
                    "fan %s" % _percent(gpu.fan_speed_pct),
                    _watts(gpu.power_w),
                    _vram(gpu.vram_used_bytes, gpu.vram_total_bytes),
                ],
            )
        )
    return entries


# -- value formatting ------------------------------------------------------


def _temperature(celsius, unit, decimals=1):
    if celsius is None:
        return MISSING
    value = convert_from_c(celsius, unit)
    return "%.*f°%s" % (decimals, value, unit)


def _percent(value):
    return MISSING if value is None else "%d%%" % round(value)


def _watts(value):
    return MISSING if value is None else "%dW" % round(value)


def _vram(used, total):
    """Render VRAM as ``used/totalGB`` in GiB, degrading if either is missing."""
    if used is None and total is None:
        return MISSING
    if total is None:
        return "%.1fGB" % (used / _GIB)
    if used is None:
        return "%s/%.0fGB" % (MISSING, total / _GIB)
    return "%.1f/%.0fGB" % (used / _GIB, total / _GIB)


# -- table helpers ---------------------------------------------------------


def _widths(rows):
    """Column widths for a list of equal-length cell lists."""
    if not rows:
        return []
    widths = [0] * max(len(row) for row in rows)
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    return widths


def _row(cells, widths, aligns, attached=()):
    """Format one row, dropping trailing whitespace from empty tail columns.

    ``attached`` columns use a single leading space, keeping ``(!)`` tight
    against its value.
    """
    line = ""
    for index, cell in enumerate(cells):
        width = widths[index] if index < len(widths) else len(cell)
        align = aligns[index] if index < len(aligns) else "<"
        padded = cell.rjust(width) if align == ">" else cell.ljust(width)
        if index:
            line += " " if index in attached else "  "
        line += padded
    return line.rstrip()


def _grid(rows, aligns, attached=()):
    widths = _widths(rows)
    return [_row(row, widths, aligns, attached) for row in rows]


# -- slack message ---------------------------------------------------------


def render_message(dashboard, headlines=(), dashboard_url=None):
    """Wrap a dashboard for Slack, with optional headlines and a Netdata link."""
    parts = []
    if headlines:
        parts.append("\n".join(headlines))
    parts.append("```\n%s\n```" % dashboard)
    if dashboard_url:
        parts.append("<%s|Full Netdata dashboard>" % dashboard_url)
    return "\n".join(parts)
