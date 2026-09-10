"""Govee sensor store and BLE adapter. No Bluetooth, hardware or ``bleak``.

The adapter is driven through :meth:`GoveeReceiver.handle_advertisement` with
stand-in device, advertisement and parser objects.
"""

from __future__ import annotations

from conftest import make_config

from lab_monitor.govee import GoveeReceiver, SensorStore, extract_values, normalise_address
from lab_monitor.models import Sensor, SensorState

NOW = 1_700_000_000.0

ROOM_A = Sensor(id="3201a", name="A", room="a", address="AA:BB:CC:00:00:01")
ROOM_B = Sensor(id="3201b", name="B", room="b", address="AA:BB:CC:00:00:02")
ROOM_C_NO_ADDRESS = Sensor(id="3201c", name="C", room="c", address=None)


def store(**kwargs):
    kwargs.setdefault("now", NOW)
    return SensorStore(**kwargs)


# -- case 1: zero sensors -------------------------------------------------


def test_a_store_with_no_traffic_is_healthy(config):
    subject = store()
    assert subject.bluetooth_error is None
    assert subject.dump() == {"established": []}


# -- case 2: configured but never seen ------------------------------------


def test_a_sensor_that_has_never_reported_is_never_seen():
    reading = store().reading(ROOM_A, NOW, 600)
    assert reading.state is SensorState.NEVER_SEEN
    assert reading.temperature_c is None
    assert reading.last_seen is None


def test_a_sensor_with_no_address_yet_is_never_seen():
    assert store().reading(ROOM_C_NO_ADDRESS, NOW, 600).state is SensorState.NEVER_SEEN


def test_it_stays_never_seen_however_long_we_wait():
    subject = store()
    for hours in range(1, 25):
        assert subject.reading(ROOM_A, NOW + hours * 3600, 600).state is SensorState.NEVER_SEEN


def test_another_sensors_traffic_does_not_establish_this_one():
    subject = store()
    subject.record(ROOM_B.address, temperature_c=24.0, now=NOW)
    assert subject.reading(ROOM_A, NOW, 600).state is SensorState.NEVER_SEEN
    assert subject.reading(ROOM_B, NOW, 600).state is SensorState.OK


# -- cases 3-5: established, stale, returning -----------------------------


def test_a_reporting_sensor_is_ok_and_carries_its_values():
    subject = store()
    subject.record(ROOM_A.address, temperature_c=24.5, humidity_pct=41.0, battery_pct=88.0, now=NOW)

    reading = subject.reading(ROOM_A, NOW + 10, 600)
    assert reading.state is SensorState.OK
    assert reading.temperature_c == 24.5
    assert reading.humidity_pct == 41.0
    assert reading.battery_pct == 88.0
    assert reading.last_seen == NOW


def test_an_established_sensor_goes_stale_after_the_timeout():
    subject = store()
    subject.record(ROOM_A.address, temperature_c=24.5, now=NOW)
    assert subject.reading(ROOM_A, NOW + 599, 600).state is SensorState.OK
    assert subject.reading(ROOM_A, NOW + 601, 600).state is SensorState.STALE


def test_a_stale_sensor_never_hands_back_its_last_value():
    """The number is still in memory; presenting it as current would lie."""
    subject = store()
    subject.record(ROOM_A.address, temperature_c=24.5, humidity_pct=41.0, now=NOW)

    reading = subject.reading(ROOM_A, NOW + 5000, 600)
    assert reading.state is SensorState.STALE
    assert reading.temperature_c is None
    assert reading.humidity_pct is None
    assert reading.battery_pct is None
    assert reading.last_seen == NOW


def test_a_returning_sensor_becomes_ok_again_with_fresh_values():
    subject = store()
    subject.record(ROOM_A.address, temperature_c=24.5, now=NOW)
    assert subject.reading(ROOM_A, NOW + 5000, 600).state is SensorState.STALE

    subject.record(ROOM_A.address, temperature_c=26.0, now=NOW + 5000)
    reading = subject.reading(ROOM_A, NOW + 5001, 600)
    assert reading.state is SensorState.OK
    assert reading.temperature_c == 26.0


def test_a_partial_update_keeps_the_fields_it_did_not_carry():
    subject = store()
    subject.record(ROOM_A.address, temperature_c=24.5, humidity_pct=41.0, now=NOW)
    subject.record(ROOM_A.address, temperature_c=25.0, now=NOW + 5)

    reading = subject.reading(ROOM_A, NOW + 6, 600)
    assert reading.temperature_c == 25.0
    assert reading.humidity_pct == 41.0


# -- restart behaviour ----------------------------------------------------


def test_establishment_survives_a_restart():
    first = store()
    first.record(ROOM_A.address, temperature_c=24.5, now=NOW)
    assert first.dump() == {"established": ["AA:BB:CC:00:00:01"]}

    restarted = SensorStore.from_state(first.dump(), now=NOW + 10)
    reading = restarted.reading(ROOM_A, NOW + 20, 600)
    assert reading.state is SensorState.PENDING
    assert reading.temperature_c is None


def test_a_restored_sensor_that_stays_silent_eventually_goes_stale():
    restarted = SensorStore.from_state({"established": [ROOM_A.address]}, now=NOW)
    assert restarted.reading(ROOM_A, NOW + 500, 600).state is SensorState.PENDING
    assert restarted.reading(ROOM_A, NOW + 700, 600).state is SensorState.STALE


def test_a_restored_sensor_that_reports_becomes_ok():
    restarted = SensorStore.from_state({"established": [ROOM_A.address]}, now=NOW)
    restarted.record(ROOM_A.address, temperature_c=23.0, now=NOW + 5)
    assert restarted.reading(ROOM_A, NOW + 10, 600).state is SensorState.OK


def test_from_state_tolerates_junk():
    for junk in (None, {}, {"established": "nope"}, {"established": [1, 2]}, "garbage"):
        assert SensorStore.from_state(junk, now=NOW).reading(ROOM_A, NOW, 600).state is (
            SensorState.NEVER_SEEN
        )


# -- bluetooth failures ---------------------------------------------------


def test_a_bluetooth_failure_is_recorded_and_cleared_by_the_next_reading():
    subject = store()
    subject.set_bluetooth_error("BleakError: no adapter")
    assert subject.bluetooth_error == "BleakError: no adapter"

    subject.record(ROOM_A.address, temperature_c=24.0, now=NOW)
    assert subject.bluetooth_error is None


def test_a_bluetooth_failure_does_not_immediately_invalidate_readings():
    """The adapter dying does not mean a reading from 5 seconds ago is wrong."""
    subject = store()
    subject.record(ROOM_A.address, temperature_c=24.0, now=NOW)
    subject.set_bluetooth_error("adapter reset")
    assert subject.reading(ROOM_A, NOW + 5, 600).state is SensorState.OK
    # ...but it does eventually go stale, like any other silence.
    assert subject.reading(ROOM_A, NOW + 700, 600).state is SensorState.STALE


# -- address handling -----------------------------------------------------


def test_addresses_are_matched_case_insensitively():
    subject = store()
    subject.record("aa:bb:cc:00:00:01", temperature_c=24.0, now=NOW)
    assert subject.reading(ROOM_A, NOW, 600).state is SensorState.OK


def test_normalise_address_handles_absent_values():
    assert normalise_address(None) is None
    assert normalise_address("  aa:bb  ") == "AA:BB"


def test_config_normalises_sensor_addresses():
    config = make_config(
        sensors=[{"id": "s", "name": "S", "room": "a", "address": "aa:bb:cc:00:00:01"}]
    )
    assert config.sensors[0].address == "AA:BB:CC:00:00:01"


# -- ble adapter ----------------------------------------------------------


class FakeDevice:
    def __init__(self, address, name="GVH5075_1234"):
        self.address = address
        self.name = name


class FakeAdvertisement:
    def __init__(self, local_name="GVH5075_1234", rssi=-58):
        self.local_name = local_name
        self.rssi = rssi
        self.manufacturer_data = {60552: b"\x00\x03\x41\x9c\x64"}
        self.service_data = {}
        self.service_uuids = []


class FakeDeviceKey:
    def __init__(self, key):
        self.key = key

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        return getattr(other, "key", None) == self.key


class FakeValue:
    def __init__(self, native_value):
        self.native_value = native_value


class FakeDescription:
    def __init__(self, unit):
        self.native_unit_of_measurement = unit


class FakeUpdate:
    def __init__(self, temperature=24.5, humidity=41.0, battery=88, unit="°C"):
        self.entity_values = {
            FakeDeviceKey("temperature"): FakeValue(temperature),
            FakeDeviceKey("humidity"): FakeValue(humidity),
            FakeDeviceKey("battery"): FakeValue(battery),
            FakeDeviceKey("signal_strength"): FakeValue(-58),
        }
        self.entity_descriptions = {FakeDeviceKey("temperature"): FakeDescription(unit)}


class FakeParser:
    """Stands in for govee_ble.GoveeBluetoothDeviceData."""

    def __init__(self, supported=True, update=None):
        self._supported = supported
        self._update = update or FakeUpdate()
        self.seen = []

    def supported(self, service_info):
        self.seen.append(service_info)
        return self._supported

    def update(self, service_info):
        return self._update


def receiver_with(parser, addresses=None, subject=None, monkeypatch=None):
    """A GoveeReceiver wired to a fake parser and a no-op service-info shim."""
    import lab_monitor.govee as govee

    subject = subject or store()
    instance = GoveeReceiver(subject, addresses=addresses)
    instance._parsers = {}
    monkeypatch.setattr(govee, "_service_info", lambda d, a: {"address": d.address})
    monkeypatch.setattr(instance, "_parser_for", lambda address: parser)
    return instance, subject


def test_a_supported_advertisement_is_recorded(monkeypatch):
    parser = FakeParser()
    receiver, subject = receiver_with(parser, monkeypatch=monkeypatch)

    values = receiver.handle_advertisement(FakeDevice(ROOM_A.address), FakeAdvertisement())

    assert values == {"temperature_c": 24.5, "humidity_pct": 41.0, "battery_pct": 88.0}
    assert subject.reading(ROOM_A, NOW + 1, 600).state is SensorState.OK


def test_an_unsupported_advertisement_is_ignored(monkeypatch):
    receiver, subject = receiver_with(FakeParser(supported=False), monkeypatch=monkeypatch)
    assert receiver.handle_advertisement(FakeDevice(ROOM_A.address), FakeAdvertisement()) is None
    assert subject.reading(ROOM_A, NOW, 600).state is SensorState.NEVER_SEEN


def test_advertisements_from_unconfigured_devices_are_filtered_out(monkeypatch):
    parser = FakeParser()
    receiver, subject = receiver_with(parser, addresses=[ROOM_A.address], monkeypatch=monkeypatch)

    assert receiver.handle_advertisement(FakeDevice("99:99:99:99:99:99"), FakeAdvertisement()) is None
    assert parser.seen == []
    assert receiver.handle_advertisement(FakeDevice(ROOM_A.address), FakeAdvertisement()) is not None


def test_a_parser_that_explodes_does_not_escape_into_bleak(monkeypatch):
    class Exploding(FakeParser):
        def update(self, service_info):
            raise ValueError("bad packet")

    receiver, subject = receiver_with(Exploding(), monkeypatch=monkeypatch)
    # _on_advertisement is what bleak calls; it must never raise.
    receiver._on_advertisement(FakeDevice(ROOM_A.address), FakeAdvertisement())
    assert subject.reading(ROOM_A, NOW, 600).state is SensorState.NEVER_SEEN


# -- value extraction ------------------------------------------------------


def test_extract_values_maps_the_keys_we_care_about():
    assert extract_values(FakeUpdate()) == {
        "temperature_c": 24.5,
        "humidity_pct": 41.0,
        "battery_pct": 88.0,
    }


def test_extract_values_converts_a_fahrenheit_report_to_celsius():
    values = extract_values(FakeUpdate(temperature=76.1, unit="°F"))
    assert round(values["temperature_c"], 1) == 24.5


def test_extract_values_ignores_non_numeric_and_absent_values():
    update = FakeUpdate()
    update.entity_values[FakeDeviceKey("temperature")] = FakeValue(None)
    update.entity_values[FakeDeviceKey("humidity")] = FakeValue("unknown")
    values = extract_values(update)
    assert "temperature_c" not in values
    assert "humidity_pct" not in values
    assert values["battery_pct"] == 88.0


def test_extract_values_survives_an_empty_update():
    class Empty:
        entity_values = {}
        entity_descriptions = {}

    assert extract_values(Empty()) == {}


def test_h5075_requires_active_scanning():
    """A passive scan silently sees nothing: the H5075 is only identifiable
    from the scan response, which passive scans never request."""
    import inspect

    import lab_monitor.govee as govee

    assert '"active"' in inspect.getsource(govee.GoveeReceiver._scan_once)
    assert "active scan" in govee.__doc__.lower()
