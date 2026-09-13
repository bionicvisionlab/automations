"""Govee sensor store and H5075 decoder. No Bluetooth, hardware or ``bleak``.

The decoder is fed raw manufacturer-data fixtures; the adapter is driven
through :meth:`GoveeReceiver.handle_advertisement` with stand-in device and
advertisement objects.
"""

from __future__ import annotations

from conftest import make_config

from lab_monitor.govee import (
    GoveeReceiver,
    SensorStore,
    decode_h5075,
    model_from_name,
    normalise_address,
)
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


# -- h5075 frame fixtures --------------------------------------------------

GOVEE_COMPANY_ID = 0xEC88

#: The documented upstream H5075 frame: 21.6 °C, 49.8 %RH, battery 100 %.
#: 0x034DB2 == 216498 == 216 * 1000 + 498.
REAL_FRAME = b"\x00\x03\x4d\xb2\x64\x00"


def frame(temperature_c, humidity_pct, battery_pct=100, prefix=0x00, status=0x00):
    """Build the six-byte H5075 manufacturer payload for a reading."""
    packed = round(abs(temperature_c) * 10) * 1000 + round(humidity_pct * 10)
    if temperature_c < 0:
        packed |= 0x800000
    return bytes([prefix]) + packed.to_bytes(3, "big") + bytes([status | battery_pct, 0x00])


def advert(payload=REAL_FRAME, company_id=GOVEE_COMPANY_ID):
    return {company_id: payload} if payload is not None else {}


def test_the_fixture_builder_reproduces_the_documented_frame():
    """Guards every other test here: the builder is not its own authority."""
    assert frame(21.6, 49.8, 100) == REAL_FRAME


# -- decoding a well-formed frame -----------------------------------------


def test_the_documented_frame_decodes_to_its_reading():
    assert decode_h5075(advert(REAL_FRAME)) == {
        "temperature_c": 21.6,
        "humidity_pct": 49.8,
        "battery_pct": 100.0,
    }


def test_a_sub_zero_temperature_decodes_as_negative():
    values = decode_h5075(advert(frame(-3.5, 62.1)))
    assert values["temperature_c"] == -3.5
    assert values["humidity_pct"] == 62.1


def test_the_freezing_point_and_a_bone_dry_room_decode():
    values = decode_h5075(advert(frame(0.0, 0.0)))
    assert values["temperature_c"] == 0.0
    assert values["humidity_pct"] == 0.0


def test_the_top_of_the_humidity_scale_decodes():
    assert decode_h5075(advert(frame(21.0, 99.9)))["humidity_pct"] == 99.9


def test_a_server_room_temperature_decodes():
    assert decode_h5075(advert(frame(31.8, 18.4)))["temperature_c"] == 31.8


def test_a_flat_battery_is_reported_rather_than_dropped():
    """0 % is a real reading and the one we most want to alert on."""
    assert decode_h5075(advert(frame(22.0, 40.0, battery_pct=0)))["battery_pct"] == 0.0


def test_a_believable_battery_is_carried_through():
    assert decode_h5075(advert(frame(22.0, 40.0, battery_pct=63)))["battery_pct"] == 63.0


def test_only_the_low_seven_bits_of_the_status_byte_are_battery():
    """The top bit is the error flag, not part of the percentage."""
    assert decode_h5075(advert(frame(22.0, 40.0, battery_pct=0x7F)))["battery_pct"] == 127.0


# -- malformed and foreign packets ----------------------------------------


def test_an_advertisement_with_no_manufacturer_data_is_not_ours():
    assert decode_h5075(None) is None
    assert decode_h5075({}) is None


def test_another_vendors_advertisement_is_not_ours():
    """An Apple iBeacon; the room is full of them."""
    assert decode_h5075({0x004C: b"\x02\x15" + b"\x00" * 21}) is None


def test_a_govee_frame_that_is_not_exactly_six_bytes_is_rejected():
    for wrong_length in (b"", b"\x00", REAL_FRAME[:3], REAL_FRAME[:5], REAL_FRAME + b"\x00"):
        assert decode_h5075(advert(wrong_length)) is None


def test_a_frame_flagging_its_own_error_is_rejected():
    """Bit 7 of the status byte means the sensor distrusts its own reading."""
    bad = frame(21.6, 49.8, battery_pct=100, status=0x80)
    assert bad[4] == 0xE4
    assert decode_h5075(advert(bad)) is None
    # ...and the same frame without the flag is the reading we trust.
    assert decode_h5075(advert(frame(21.6, 49.8, battery_pct=100))) is not None


def test_a_frame_with_the_wrong_prefix_byte_is_rejected():
    assert decode_h5075(advert(frame(21.6, 49.8, prefix=0x01))) is None


def test_a_frame_decoding_to_an_impossible_temperature_is_rejected():
    """0x7FFFFF would read as 838.8 °C -- that is a misread, not a fire."""
    assert decode_h5075(advert(b"\x00\x7f\xff\xff\x64\x00")) is None
    assert decode_h5075(advert(b"\x00\xff\xff\xff\x64\x00")) is None


def test_a_bytearray_payload_decodes_like_bytes():
    assert decode_h5075(advert(bytearray(REAL_FRAME)))["temperature_c"] == 21.6


# -- model names for discovery --------------------------------------------


def test_the_model_comes_from_the_advertised_name():
    assert model_from_name("GVH5075_1234") == "H5075"


def test_an_unhelpful_name_falls_back_to_the_only_model_we_decode():
    for name in (None, "", "Unknown", "GV"):
        assert model_from_name(name) == "H5075"


# -- ble adapter ----------------------------------------------------------


class FakeDevice:
    def __init__(self, address, name="GVH5075_1234"):
        self.address = address
        self.name = name


class FakeAdvertisement:
    def __init__(self, manufacturer_data=None, local_name="GVH5075_1234", rssi=-58):
        self.local_name = local_name
        self.rssi = rssi
        self.manufacturer_data = advert() if manufacturer_data is None else manufacturer_data
        self.service_data = {}
        self.service_uuids = []


def receiver_with(addresses=None, subject=None):
    subject = subject or store()
    return GoveeReceiver(subject, addresses=addresses), subject


def test_a_supported_advertisement_is_recorded():
    receiver, subject = receiver_with()

    values = receiver.handle_advertisement(FakeDevice(ROOM_A.address), FakeAdvertisement())

    assert values == {"temperature_c": 21.6, "humidity_pct": 49.8, "battery_pct": 100.0}
    assert subject.reading(ROOM_A, NOW + 1, 600).state is SensorState.OK


def test_an_unsupported_advertisement_is_ignored():
    receiver, subject = receiver_with()
    advertisement = FakeAdvertisement(manufacturer_data={0x004C: b"\x02\x15"})
    assert receiver.handle_advertisement(FakeDevice(ROOM_A.address), advertisement) is None
    assert subject.reading(ROOM_A, NOW, 600).state is SensorState.NEVER_SEEN


def test_advertisements_from_unconfigured_devices_are_filtered_out():
    receiver, subject = receiver_with(addresses=[ROOM_A.address])

    assert (
        receiver.handle_advertisement(FakeDevice("99:99:99:99:99:99"), FakeAdvertisement()) is None
    )
    assert subject.dump() == {"established": []}
    assert receiver.handle_advertisement(FakeDevice(ROOM_A.address), FakeAdvertisement()) is not None


def test_a_decode_that_explodes_does_not_escape_into_bleak():
    class Exploding:
        @property
        def manufacturer_data(self):
            raise ValueError("bad packet")

    receiver, subject = receiver_with()
    # _on_advertisement is what bleak calls; it must never raise.
    receiver._on_advertisement(FakeDevice(ROOM_A.address), Exploding())
    assert subject.reading(ROOM_A, NOW, 600).state is SensorState.NEVER_SEEN


def test_an_advertisement_without_manufacturer_data_is_ignored():
    receiver, subject = receiver_with()
    advertisement = FakeAdvertisement(manufacturer_data={})
    assert receiver.handle_advertisement(FakeDevice(ROOM_A.address), advertisement) is None
    assert subject.reading(ROOM_A, NOW, 600).state is SensorState.NEVER_SEEN


def test_h5075_requires_active_scanning():
    """A passive scan silently sees nothing: the H5075 carries its readings in
    the scan response, which passive scans never request."""
    import inspect

    import lab_monitor.govee as govee

    assert '"active"' in inspect.getsource(govee.GoveeReceiver._scan_once)
    assert '"active"' in inspect.getsource(govee._discover_async)
    assert "active" in govee.__doc__.lower()
