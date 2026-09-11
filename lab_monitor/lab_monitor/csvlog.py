"""Raw per-poll telemetry as a CSV append log.

One row per polling cycle, holding the measurements as they were read: no
thresholds, no transitions, no aggregation, nothing derived. Netdata remains
the history and charting layer; this file exists so the raw room and GPU
numbers can be re-analysed offline without going through it.

A value we do not currently have is an empty cell, never a repeat of the last
one we did have. "The room was 24.1 C" and "we did not know what the room was"
have to stay distinguishable a year later, so a stale sensor, an unavailable
machine, a metric the driver never reported, and a Govee reading already
written all produce blanks.

Sensor columns come from the configuration, so they are known before the first
row. GPU columns cannot be: each card is discovered through Netdata, and an
offline machine reports none. So the GPU column set is seeded from the header
of the file we are appending to and only ever grows -- a machine that is down
writes blanks under the columns it already has, and a genuinely new card moves
the old file aside and starts a fresh one.
"""

from __future__ import annotations

import csv
import datetime
import os

from .models import SensorState

#: First column of every row: when the snapshot was taken.
TIMESTAMP_COLUMN = "timestamp"

#: Per-sensor measurements, as attributes of :class:`~lab_monitor.models.SensorReading`.
SENSOR_FIELDS = ("temperature_c", "humidity_pct", "battery_pct")

#: Per-GPU measurements, as attributes of :class:`~lab_monitor.models.GpuReading`.
GPU_FIELDS = (
    "temperature_c",
    "utilization_pct",
    "fan_speed_pct",
    "power_w",
    "vram_used_bytes",
    "vram_total_bytes",
)


class CsvLog:
    """Appends one row per poll to a CSV whose header is fixed per file.

    :attr:`path` is always the current file. A schema change moves the old file
    aside under a timestamped name rather than widening it, so a restart
    resumes the current schema instead of rediscovering a superseded one.
    """

    def __init__(self, path, sensor_ids=(), machine_ids=()):
        self.path = str(path)
        self.sensor_ids = tuple(sensor_ids)
        self.machine_ids = tuple(machine_ids)
        self._header = None
        self._gpus = {}
        self._logged = {}

    # -- writing ----------------------------------------------------------

    def append(self, snapshot):
        """Append one row for ``snapshot``. Returns the path written."""
        if self._header is None:
            self._adopt_existing()

        self._learn(snapshot)
        fresh = self._unlogged_readings(snapshot)
        fields = self._fields(snapshot, fresh)
        columns = [TIMESTAMP_COLUMN] + [name for name, _ in fields]
        if columns != self._header:
            self._start(columns, snapshot.taken_at)

        row = [_timestamp(snapshot.taken_at)] + [_format(value) for _, value in fields]
        with open(self.path, "a", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(row)
            handle.flush()

        # Only once the row is on disk, so a failed write does not lose a reading.
        self._logged.update(fresh)
        return self.path

    def _start(self, columns, now):
        """Begin a file with this header at the configured path.

        A file with a different header is moved aside rather than appended to:
        mixing schemas would misalign every row after the join. The configured
        path always holds the current schema, so a restart resumes it rather
        than rediscovering a superseded one and archiving all over again.
        """
        if self._header:
            os.replace(self.path, _archive_path(self.path, now))
        self._header = columns

        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(columns)
            handle.flush()

    # -- schema -----------------------------------------------------------

    def _adopt_existing(self):
        """Read the header of the file we mean to append to, if there is one."""
        self._header = []
        try:
            with open(self.path, "r", newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle), None)
        except FileNotFoundError:
            return
        if not header:
            return

        self._header = header
        for machine_id, index in _gpu_columns(header):
            self._remember(machine_id, index)

    def _learn(self, snapshot):
        """Note every GPU this snapshot saw, so its columns exist from now on."""
        for reading in snapshot.machines:
            for gpu in reading.gpus:
                self._remember(reading.machine_id, gpu.index)

    def _remember(self, machine_id, index):
        indexes = self._gpus.setdefault(machine_id, [])
        if index not in indexes:
            indexes.append(index)
            indexes.sort(key=_index_sort_key)

    # -- rows -------------------------------------------------------------

    def _unlogged_readings(self, snapshot):
        """``{sensor_id: last_seen}`` for sensors that reported since we last logged.

        A Govee broadcast counts as current until the staleness timeout expires,
        so ``SensorStore`` hands back one 3:00pm reading on every poll for the
        next ten minutes. Writing it each time would fabricate twenty
        measurements out of one, biasing exactly the means and
        time-above-threshold sums this log exists to support. ``last_seen`` is
        the broadcast's own timestamp, so it is what says whether anything new
        actually arrived.
        """
        fresh = {}
        for sensor_id in self.sensor_ids:
            reading = snapshot.sensor(sensor_id)
            if reading is None or reading.state is not SensorState.OK:
                continue
            # An unknown last_seen cannot be shown to be a repeat, so it is logged.
            if reading.last_seen is not None and reading.last_seen == self._logged.get(sensor_id):
                continue
            fresh[sensor_id] = reading.last_seen
        return fresh

    def _fields(self, snapshot, fresh):
        """The value columns as ``(name, value)`` pairs, header and row together.

        Built in one pass so a column can never drift away from its cell.
        """
        fields = []
        for sensor_id in self.sensor_ids:
            reading = snapshot.sensor(sensor_id) if sensor_id in fresh else None
            for field in SENSOR_FIELDS:
                value = None if reading is None else getattr(reading, field)
                fields.append(("sensor.%s.%s" % (sensor_id, field), value))

        for machine_id in self.machine_ids:
            indexes = self._gpus.get(machine_id, ())
            if not indexes:
                continue
            reading = snapshot.machine(machine_id)
            gpus = {}
            if reading is not None and reading.available:
                gpus = {gpu.index: gpu for gpu in reading.gpus}
            for index in indexes:
                gpu = gpus.get(index)
                for field in GPU_FIELDS:
                    value = None if gpu is None else getattr(gpu, field)
                    fields.append(("gpu.%s.%s.%s" % (machine_id, index, field), value))
        return fields


# -- column names ----------------------------------------------------------


def _gpu_columns(header):
    """Yield ``(machine_id, index)`` for the GPU columns in a header.

    Split from the right: a machine id may contain dots, a field name never
    does. Anything we do not recognise is ignored -- it will not be reproduced
    by :meth:`CsvLog._fields`, so the header comparison rejects the file.
    """
    for name in header:
        if not name.startswith("gpu."):
            continue
        parts = name[len("gpu."):].rsplit(".", 2)
        if len(parts) == 3 and parts[2] in GPU_FIELDS:
            yield parts[0], parts[1]


def _index_sort_key(index):
    """Order GPU indexes numerically when we can, alphabetically otherwise."""
    try:
        return (0, int(index), "")
    except (TypeError, ValueError):
        return (1, 0, str(index))


# -- values ----------------------------------------------------------------


def _timestamp(taken_at):
    """Timezone-aware ISO-8601, so a row stays unambiguous across DST moves."""
    moment = datetime.datetime.fromtimestamp(taken_at, datetime.timezone.utc).astimezone()
    return moment.isoformat(timespec="seconds")


def _format(value):
    """Render one measurement compactly; unknown is blank, never zero."""
    if value is None:
        return ""
    number = float(value)
    if number.is_integer():
        return "%d" % number
    return ("%.3f" % number).rstrip("0").rstrip(".")


def _archive_path(path, now):
    """Where a superseded schema goes: ``lab_monitor-20260910T154200.csv``."""
    stamp = datetime.datetime.fromtimestamp(now).strftime("%Y%m%dT%H%M%S")
    base, extension = os.path.splitext(path)
    candidate = "%s-%s%s" % (base, stamp, extension)
    serial = 1
    while os.path.exists(candidate):
        candidate = "%s-%s-%d%s" % (base, stamp, serial, extension)
        serial += 1
    return candidate
