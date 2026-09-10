"""Govee BLE room sensors.

:class:`SensorStore` is pure bookkeeping -- when each address last reported
and whether it is healthy -- with no Bluetooth imports, so the staleness logic
is testable. :class:`GoveeReceiver` is the hardware adapter, running a bleak
active scan on a background thread that feeds ``govee-ble``.

``bleak`` and ``govee_ble`` are imported lazily, so the module works without
the BLE extras installed.

Scanning is always active: ``govee-ble`` marks the H5075
``requires_active_scan=True`` because its model is only identifiable from the
scan response, which passive scans never request.
"""

from __future__ import annotations

import threading
import time

from .models import SensorReading, SensorState

#: How long to wait before restarting a scan that failed.
BLUETOOTH_RETRY_SECONDS = 30.0

#: Source name handed to govee-ble; it only uses this to key its own caches.
BLE_SOURCE = "lab_monitor"

#: Default duration of ``discover-govee``.
DISCOVERY_SECONDS = 30.0


def normalise_address(address):
    """One spelling for BLE MACs, so config and adapter always match."""
    if not address:
        return None
    return str(address).strip().upper()


class SensorStore:
    """Tracks the last reading from each Govee address.

    Thread-safe: the BLE thread writes, the poll loop reads. This store, not
    Netdata, is the authority on sensor freshness.
    """

    def __init__(self, established=None, now=None, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._live = {}
        self._established = set()
        self._started_at = clock() if now is None else now
        self.bluetooth_error = None

        for address in established or ():
            normalised = normalise_address(address)
            if normalised:
                self._established.add(normalised)

    # -- writes -----------------------------------------------------------

    def record(self, address, temperature_c=None, humidity_pct=None, battery_pct=None, now=None):
        """Store a fresh reading for one device."""
        normalised = normalise_address(address)
        if not normalised:
            return
        timestamp = self._clock() if now is None else now
        with self._lock:
            entry = self._live.setdefault(normalised, {})
            entry["last_seen"] = timestamp
            if temperature_c is not None:
                entry["temperature_c"] = float(temperature_c)
            if humidity_pct is not None:
                entry["humidity_pct"] = float(humidity_pct)
            if battery_pct is not None:
                entry["battery_pct"] = float(battery_pct)
            self._established.add(normalised)
            self.bluetooth_error = None

    def set_bluetooth_error(self, message):
        """Record that the BLE stack itself is unhappy."""
        with self._lock:
            self.bluetooth_error = message

    # -- reads ------------------------------------------------------------

    def reading(self, sensor, now, timeout_seconds):
        """Current :class:`SensorReading` for a configured sensor.

        A stale sensor comes back with no values at all.
        """
        address = normalise_address(sensor.address)
        if not address:
            return SensorReading(sensor_id=sensor.id, state=SensorState.NEVER_SEEN)

        with self._lock:
            entry = dict(self._live.get(address, {}))
            established = address in self._established

        if not established:
            return SensorReading(sensor_id=sensor.id, state=SensorState.NEVER_SEEN)

        # Restored-from-disk sensors have no reading yet, so their grace
        # period runs from service start.
        last_seen = entry.get("last_seen")
        reference = last_seen if last_seen is not None else self._started_at
        if (now - reference) > timeout_seconds:
            return SensorReading(
                sensor_id=sensor.id,
                state=SensorState.STALE,
                last_seen=last_seen,
            )

        if last_seen is None:
            return SensorReading(sensor_id=sensor.id, state=SensorState.PENDING)

        return SensorReading(
            sensor_id=sensor.id,
            state=SensorState.OK,
            temperature_c=entry.get("temperature_c"),
            humidity_pct=entry.get("humidity_pct"),
            battery_pct=entry.get("battery_pct"),
            last_seen=last_seen,
        )

    def dump(self):
        """Persistable state: which addresses we have ever heard from.

        Establishment only, never values -- so a sensor that dies during a
        reboot is still reported, without resurrecting a stale temperature.
        """
        with self._lock:
            return {"established": sorted(self._established)}

    @classmethod
    def from_state(cls, raw, now=None, clock=time.time):
        """Rebuild from :meth:`dump` output, tolerating anything else."""
        established = ()
        if isinstance(raw, dict):
            values = raw.get("established")
            if isinstance(values, list):
                established = [v for v in values if isinstance(v, str)]
        return cls(established=established, now=now, clock=clock)


class GoveeReceiver:
    """Background BLE active scanner that feeds a :class:`SensorStore`.

    Runs its own asyncio loop on a daemon thread, keeping the main service a
    plain synchronous poll loop. Failed scans retry forever and surface the
    error on the store.
    """

    def __init__(self, store, addresses=None, retry_seconds=BLUETOOTH_RETRY_SECONDS, logger=None):
        self.store = store
        self.addresses = {a for a in (normalise_address(x) for x in addresses or ()) if a}
        self.retry_seconds = retry_seconds
        self.logger = logger
        self._thread = None
        self._stop = threading.Event()
        self._parsers = {}

    # -- lifecycle --------------------------------------------------------

    def start(self):
        """Start scanning on a background thread. Safe to call once."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="govee-ble", daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        """Ask the scanner to stop and wait briefly for the thread to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self):
        import asyncio

        try:
            asyncio.run(self._scan_forever())
        except Exception as exc:  # pragma: no cover - defensive
            self.store.set_bluetooth_error(str(exc))
            self._log("BLE scanner stopped: %s", exc)

    async def _scan_forever(self):
        import asyncio

        while not self._stop.is_set():
            try:
                await self._scan_once()
            except Exception as exc:
                self.store.set_bluetooth_error("%s: %s" % (type(exc).__name__, exc))
                self._log("BLE scan failed (%s); retrying in %.0fs", exc, self.retry_seconds)
                await asyncio.sleep(self.retry_seconds)

    async def _scan_once(self):
        import asyncio

        from bleak import BleakScanner

        scanner = BleakScanner(
            detection_callback=self._on_advertisement,
            scanning_mode="active",
        )
        async with scanner:
            while not self._stop.is_set():
                await asyncio.sleep(0.5)

    # -- parsing ----------------------------------------------------------

    def _on_advertisement(self, device, advertisement_data):
        """Handle one BLE advertisement. Never raises into bleak's callback."""
        try:
            self.handle_advertisement(device, advertisement_data)
        except Exception as exc:  # pragma: no cover - defensive
            self._log("ignoring bad advertisement from %s: %s", device.address, exc)

    def handle_advertisement(self, device, advertisement_data):
        """Parse an advertisement and record it if it is a sensor we want."""
        address = normalise_address(device.address)
        if self.addresses and address not in self.addresses:
            return None

        service_info = _service_info(device, advertisement_data)
        parser = self._parser_for(address)
        if not parser.supported(service_info):
            return None

        update = parser.update(service_info)
        values = extract_values(update)
        if not values:
            return None
        self.store.record(address, **values)
        return values

    def _parser_for(self, address):
        """One parser per device; govee-ble keeps per-device decode state."""
        parser = self._parsers.get(address)
        if parser is None:
            from govee_ble import GoveeBluetoothDeviceData

            parser = GoveeBluetoothDeviceData()
            self._parsers[address] = parser
        return parser

    def _log(self, message, *args):
        if self.logger is not None:
            self.logger.warning(message, *args)


def _service_info(device, advertisement_data):
    """Wrap a bleak device/advertisement pair for the govee-ble parser."""
    from habluetooth import BluetoothServiceInfo

    return BluetoothServiceInfo.from_advertisement(device, advertisement_data, BLE_SOURCE)


def extract_values(update):
    """Pull temperature/humidity/battery out of a govee-ble ``SensorUpdate``.

    Module-level and import-free so it is testable with a fake update.
    """
    values = {}
    entity_values = getattr(update, "entity_values", None) or {}
    descriptions = getattr(update, "entity_descriptions", None) or {}

    for device_key, sensor_value in entity_values.items():
        key = getattr(device_key, "key", None)
        native = getattr(sensor_value, "native_value", None)
        if native is None or isinstance(native, str):
            continue

        if key == "temperature":
            unit = _unit_of(descriptions.get(device_key))
            celsius = float(native)
            if unit and "F" in str(unit).upper() and "C" not in str(unit).upper():
                celsius = (celsius - 32.0) * 5.0 / 9.0
            values["temperature_c"] = celsius
        elif key == "humidity":
            values["humidity_pct"] = float(native)
        elif key == "battery":
            values["battery_pct"] = float(native)

    return values


def _unit_of(description):
    return getattr(description, "native_unit_of_measurement", None)


# -- discovery (setup utility, not part of the daemon) ---------------------


def discover(duration=DISCOVERY_SECONDS, printer=print):
    """Active-scan for nearby Govee sensors and print what we can identify."""
    import asyncio

    found = asyncio.run(_discover_async(duration))
    if not found:
        printer("No supported Govee devices seen in %.0f seconds." % duration)
        printer("")
        printer("Check that Bluetooth is up (`bluetoothctl show`) and that the")
        printer("sensors are powered and within range.")
        return found

    printer("Found %d Govee device(s):" % len(found))
    printer("")
    for entry in sorted(found.values(), key=lambda e: e["address"]):
        printer("  address     %s" % entry["address"])
        printer("  name        %s" % (entry.get("name") or "?"))
        printer("  model       %s" % (entry.get("model") or "?"))
        printer("  rssi        %s dBm" % entry.get("rssi"))
        if entry.get("temperature_c") is not None:
            printer("  temperature %.1f °C" % entry["temperature_c"])
        if entry.get("humidity_pct") is not None:
            printer("  humidity    %.0f %%" % entry["humidity_pct"])
        if entry.get("battery_pct") is not None:
            printer("  battery     %.0f %%" % entry["battery_pct"])
        printer("")

    printer("Add each one to your lab_monitor.toml, for example:")
    printer("")
    for entry in sorted(found.values(), key=lambda e: e["address"]):
        printer("  [[sensors]]")
        printer("  id = \"3201a\"")
        printer("  name = \"BioE 3201A sensor\"")
        printer("  room = \"a\"")
        printer("  address = \"%s\"" % entry["address"])
        printer("")
    return found


async def _discover_async(duration):
    import asyncio

    from bleak import BleakScanner
    from govee_ble import GoveeBluetoothDeviceData

    found = {}
    parsers = {}

    def on_advertisement(device, advertisement_data):
        address = normalise_address(device.address)
        try:
            service_info = _service_info(device, advertisement_data)
            parser = parsers.setdefault(address, GoveeBluetoothDeviceData())
            if not parser.supported(service_info):
                return
            update = parser.update(service_info)
        except Exception:
            return

        entry = found.setdefault(address, {"address": address})
        entry["rssi"] = advertisement_data.rssi
        entry["name"] = advertisement_data.local_name or device.name
        entry["model"] = _model_of(update, parser)
        entry.update(extract_values(update))

    scanner = BleakScanner(detection_callback=on_advertisement, scanning_mode="active")
    async with scanner:
        await asyncio.sleep(duration)
    return found


def _model_of(update, parser):
    """Best-effort model string for discovery output."""
    devices = getattr(update, "devices", None) or {}
    for info in devices.values():
        model = getattr(info, "model", None)
        if model:
            return model
    return getattr(parser, "device_type", None)
