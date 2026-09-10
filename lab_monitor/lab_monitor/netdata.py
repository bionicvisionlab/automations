"""Netdata adapter: GPU/machine state out, room readings in.

:class:`NetdataClient` queries the Parent's ``/api/v3/data``;
:class:`StatsdEmitter` pushes BLE room readings to the local StatsD listener.

Availability comes from ``db.last_entry`` (the newest timestamp actually held
for the queried metrics), not from the presence of a value -- Netdata serves
the last stored point for a machine that died an hour ago.

API shape, per the current OpenAPI spec:

* ``format=json2`` returns ``result.labels`` (``"time"`` first),
  ``result.point`` (index map; bit 0 of ``pa`` means EMPTY), and
  ``result.data`` rows of ``[timestamp, point, point, ...]``.
* ``group_by=label`` + ``group_by_label=index`` gives one dimension per GPU.
* ``options`` is one string, split on ``,``, space or ``|``.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .models import GpuReading, MachineReading

#: nvidia_smi contexts mapped onto :class:`GpuReading` fields, as
#: ``(field, context, aggregation, dimension filter)``. VRAM needs the filter
#: because its context reports free/used/reserved together.
GPU_METRICS = (
    ("temperature_c", "nvidia_smi.gpu_temperature", "average", None),
    ("utilization_pct", "nvidia_smi.gpu_utilization", "average", None),
    ("fan_speed_pct", "nvidia_smi.gpu_fan_speed_perc", "average", None),
    ("power_w", "nvidia_smi.gpu_power_draw", "average", None),
    ("vram_used_bytes", "nvidia_smi.gpu_frame_buffer_memory_usage", "sum", "used"),
    ("vram_total_bytes", "nvidia_smi.gpu_frame_buffer_memory_usage", "sum", None),
)

#: Heartbeat context. Every agent collects it, so its newest timestamp says
#: whether the machine is still streaming.
HEARTBEAT_CONTEXT = "system.cpu"

#: Bit 0 of a json2 point's ``pa`` annotation: the point has no value.
POINT_EMPTY = 1


class NetdataError(Exception):
    """Raised when Netdata cannot be reached or returns something unusable."""


class NetdataClient:
    """Thin read-only client for a Netdata agent's v3 API.

    ``fetch`` takes a URL and returns decoded JSON; tests inject fixtures.
    """

    def __init__(self, base_url, timeout=5.0, fetch=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._fetch = fetch or self._http_get

    # -- transport --------------------------------------------------------

    def _http_get(self, url):
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise NetdataError("%s returned HTTP %s" % (url, exc.code)) from None
        except urllib.error.URLError as exc:
            raise NetdataError("could not reach %s: %s" % (url, exc.reason)) from None
        except (ValueError, socket.timeout) as exc:
            raise NetdataError("bad response from %s: %s" % (url, exc)) from None

    def get(self, path, params=None):
        """GET a JSON document from the agent."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = self._fetch(url)
        if not isinstance(payload, dict):
            raise NetdataError("%s did not return a JSON object" % url)
        return payload

    # -- queries ----------------------------------------------------------

    def data(self, hostname, context, window_seconds, aggregation="average", dimensions=None):
        """Run a single current-value query scoped to one node and context."""
        params = {
            "scope_nodes": hostname,
            "scope_contexts": context,
            "group_by": "label",
            "group_by_label": "index",
            "aggregation": aggregation,
            "after": -int(window_seconds),
            "before": 0,
            "points": 1,
            "time_group": "average",
            "format": "json2",
            "options": "jsonwrap|minify|group-by-labels|seconds",
        }
        if dimensions:
            params["dimensions"] = dimensions
        return self.get("/api/v3/data", params)

    def nodes(self):
        """List the nodes this agent serves (itself plus any children)."""
        payload = self.get("/api/v3/nodes")
        nodes = payload.get("nodes")
        return nodes if isinstance(nodes, list) else []


# -- json2 parsing ---------------------------------------------------------


def latest_values(payload):
    """Extract ``{dimension_id: value}`` from a ``format=json2`` response.

    Scans rows newest-first, since partial-data trimming can leave the final
    row blank. Dimensions with no usable point are absent, never zero.
    """
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}

    labels = result.get("labels")
    rows = result.get("data")
    if not isinstance(labels, list) or not isinstance(rows, list):
        return {}

    point_map = result.get("point") if isinstance(result.get("point"), dict) else {}
    value_index = point_map.get("value", 0)
    pa_index = point_map.get("pa")

    values = {}
    for row in reversed(rows):
        if not isinstance(row, list) or len(row) < 2:
            continue
        for column, dimension in enumerate(labels[1:], start=1):
            if dimension in values or column >= len(row):
                continue
            value = _point_value(row[column], value_index, pa_index)
            if value is not None:
                values[dimension] = value
        if len(values) == len(labels) - 1:
            break
    return values


def _point_value(point, value_index, pa_index):
    """Pull the numeric value out of one json2 point, honouring the EMPTY bit."""
    if isinstance(point, (int, float)) and not isinstance(point, bool):
        return float(point)
    if not isinstance(point, list) or not point:
        return None
    if pa_index is not None and pa_index < len(point):
        annotation = point[pa_index]
        if isinstance(annotation, (int, float)) and int(annotation) & POINT_EMPTY:
            return None
    if value_index >= len(point):
        return None
    value = point[value_index]
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def dimension_labels(payload, key):
    """Return ``{dimension_id: label_value}`` for one label key, if present.

    Requires the ``group-by-labels`` option; absent on older agents.
    """
    view = payload.get("view")
    if not isinstance(view, dict):
        return {}
    dimensions = view.get("dimensions")
    if not isinstance(dimensions, dict):
        return {}
    ids = dimensions.get("ids")
    labels = dimensions.get("labels")
    if not isinstance(ids, list) or not isinstance(labels, dict):
        return {}
    values = labels.get(key)
    if not isinstance(values, list):
        return {}

    out = {}
    for dimension_id, entry in zip(ids, values):
        if isinstance(entry, list) and entry:
            out[dimension_id] = str(entry[0])
        elif isinstance(entry, str):
            out[dimension_id] = entry
    return out


def last_entry(payload):
    """The newest timestamp Netdata actually holds for the queried metrics."""
    db = payload.get("db")
    if not isinstance(db, dict):
        return None
    value = db.get("last_entry")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


# -- machine collection ----------------------------------------------------


def collect_machine(client, machine, now, timeout_seconds):
    """Build a :class:`MachineReading` for one workstation.

    Availability gates everything: a machine with no fresh data is reported
    unavailable with no GPUs, rather than serving stale values as current.
    """
    host = machine.netdata_hostname
    window = max(int(timeout_seconds), 60)

    try:
        heartbeat = client.data(host, HEARTBEAT_CONTEXT, window)
    except (NetdataError, OSError) as exc:
        return MachineReading(machine_id=machine.id, available=False, error=str(exc))

    seen = last_entry(heartbeat)
    if seen is None or (now - seen) > timeout_seconds:
        return MachineReading(
            machine_id=machine.id,
            available=False,
            last_seen=seen,
            error=None if seen else "no data in Netdata for host %r" % host,
        )

    gpus, error = _collect_gpus(client, host, window)
    return MachineReading(
        machine_id=machine.id,
        available=True,
        last_seen=seen,
        gpus=gpus,
        error=error,
    )


def _collect_gpus(client, host, window):
    """Query every GPU context and pivot the results into per-GPU readings.

    Contexts are queried independently, so a driver missing one of them costs
    that field alone.
    """
    fields = {}
    product_names = {}
    error = None

    for field_name, context, aggregation, dimensions in GPU_METRICS:
        try:
            payload = client.data(host, context, window, aggregation, dimensions)
        except (NetdataError, OSError) as exc:
            error = error or str(exc)
            continue
        fields[field_name] = latest_values(payload)
        product_names.update(dimension_labels(payload, "product_name"))

    indexes = set()
    for values in fields.values():
        indexes.update(values)
    if not indexes:
        return (), error

    gpus = []
    for index in sorted(indexes, key=_index_sort_key):
        gpus.append(
            GpuReading(
                index=index,
                product_name=product_names.get(index),
                temperature_c=fields.get("temperature_c", {}).get(index),
                utilization_pct=fields.get("utilization_pct", {}).get(index),
                fan_speed_pct=fields.get("fan_speed_pct", {}).get(index),
                power_w=fields.get("power_w", {}).get(index),
                vram_used_bytes=fields.get("vram_used_bytes", {}).get(index),
                vram_total_bytes=fields.get("vram_total_bytes", {}).get(index),
            )
        )
    return tuple(gpus), error


def _index_sort_key(index):
    """Sort GPU indexes numerically when we can, alphabetically otherwise."""
    try:
        return (0, int(index), "")
    except (TypeError, ValueError):
        return (1, 0, str(index))


# -- statsd export ---------------------------------------------------------


class StatsdEmitter:
    """Push room readings to the local Netdata StatsD listener over UDP.

    Fire-and-forget: Netdata is the history layer, not the source of truth for
    sensor liveness, so a lost datagram affects nothing and is not raised.
    """

    def __init__(self, host="127.0.0.1", port=8125, prefix="labmonitor", enabled=True, send=None):
        self.host = host
        self.port = port
        self.prefix = prefix.rstrip(".")
        self.enabled = enabled
        self._send = send
        self._socket = None
        self.last_error = None

    def gauge(self, name, value):
        """Send one gauge sample. Returns True if it was handed to the socket."""
        if not self.enabled or value is None:
            return False
        line = "%s.%s:%s|g" % (self.prefix, name, _format_gauge(value))
        return self._emit(line)

    def room_reading(self, room_id, temperature_c=None, humidity_pct=None, battery_pct=None):
        """Send whichever of a room's metrics we currently have."""
        sent = False
        base = "room.%s" % room_id
        if temperature_c is not None:
            sent |= self.gauge("%s.temperature_c" % base, temperature_c)
        if humidity_pct is not None:
            sent |= self.gauge("%s.humidity_pct" % base, humidity_pct)
        if battery_pct is not None:
            sent |= self.gauge("%s.battery_pct" % base, battery_pct)
        return sent

    def _emit(self, line):
        try:
            if self._send is not None:
                self._send(line)
                return True
            if self._socket is None:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.sendto(line.encode("utf-8"), (self.host, self.port))
            self.last_error = None
            return True
        except OSError as exc:
            self.last_error = str(exc)
            self._socket = None
            return False

    def close(self):
        """Release the UDP socket, if one was opened."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None


def _format_gauge(value):
    """Render a gauge value compactly, without scientific notation."""
    return ("%.3f" % float(value)).rstrip("0").rstrip(".") or "0"
