"""Snapshot assembly and the text dashboard."""

from __future__ import annotations

from conftest import (
    BASE_CONFIG,
    f_to_c,
    gpu,
    machine,
    make_config,
    sensor_ok,
    sensor_state,
    snapshot,
)

from lab_monitor.alerts import EMPTY_ASSESSMENT, AlertEngine
from lab_monitor.models import SensorState
from lab_monitor.status import build_snapshot, render_dashboard, render_message

NOW = 1_700_000_000.0


def render(config, snap, assessment=EMPTY_ASSESSMENT):
    return render_dashboard(config, snap, assessment)


def assess(config, snap, states=None):
    return AlertEngine(config, states).evaluate(snap)


# -- case 1: zero Govee sensors configured --------------------------------


def test_dashboard_works_with_no_sensors_configured(config, all_available):
    text = render(config, all_available(NOW))
    assert "ENVIRONMENT" in text
    assert "No room sensors configured" in text
    assert "COMPUTE" in text
    assert "gpu2" in text


def test_header_shows_site_and_time(config, all_available):
    text = render(config, all_available(NOW))
    first = text.splitlines()[0]
    assert first.startswith("BioE 3201")
    assert ":" in first  # the clock
    assert text.splitlines()[1].startswith("─")


def test_machines_are_grouped_by_room_in_display_order(config, all_available):
    text = render(config, all_available(NOW))
    lines = [line for line in text.splitlines() if line]
    order = [line for line in lines if line in ("BioE 3201A", "BioE 3201D", "Foyer")]
    assert order == ["BioE 3201A", "BioE 3201D", "Foyer"]
    # Rooms with no machines are not given an empty COMPUTE heading.
    assert "BioE 3201B" not in lines
    assert "BioE 3201C" not in lines


def test_gpu_line_shows_every_metric(config):
    snap = snapshot(
        NOW,
        machines=[
            machine(
                "gpu2",
                gpus=[
                    gpu(
                        "0",
                        temperature_c=73.0,
                        utilization_pct=96.0,
                        fan_speed_pct=71.0,
                        power_w=382.0,
                        vram_used_bytes=18.2 * 1024 ** 3,
                        vram_total_bytes=24.0 * 1024 ** 3,
                    )
                ],
            )
        ],
    )
    line = _find(render(config, snap), "gpu2")
    assert "GPU0" in line
    assert "73°C" in line
    assert "96%" in line
    assert "fan 71%" in line
    assert "382W" in line
    assert "18.2/24GB" in line


# -- case 12: multiple GPUs on one machine --------------------------------


def test_multiple_gpus_each_get_a_line_under_one_machine_name(config):
    snap = snapshot(
        NOW,
        machines=[machine("gpu2", gpus=[gpu("0", 70.0), gpu("1", 65.0), gpu("2", 60.0)])],
    )
    text = render(config, snap)
    gpu_lines = [line for line in text.splitlines() if "GPU" in line]
    assert len(gpu_lines) == 3
    assert "gpu2" in gpu_lines[0]
    # The machine name appears once, not once per card.
    assert text.count("gpu2") == 1
    assert [label for label in ("GPU0", "GPU1", "GPU2") if label in text] == [
        "GPU0",
        "GPU1",
        "GPU2",
    ]


# -- case 11: multiple machines in one room -------------------------------


def test_two_machines_in_one_room_share_a_single_room_heading():
    config = make_config(
        machines=BASE_CONFIG["machines"] + [
            {"id": "gpu4", "name": "gpu4", "room": "a", "netdata_hostname": "gpu4"}
        ]
    )
    snap = snapshot(
        NOW,
        machines=[
            machine("gpu2", gpus=[gpu("0", 70.0)]),
            machine("gpu4", gpus=[gpu("0", 61.0)]),
        ],
    )
    text = render(config, snap)
    assert text.count("BioE 3201A") == 1
    body = text.split("BioE 3201A", 1)[1]
    assert body.index("gpu2") < body.index("gpu4")


# -- case 13: missing optional GPU metric ---------------------------------


def test_missing_fan_speed_does_not_break_the_dashboard(config):
    snap = snapshot(
        NOW,
        machines=[machine("gpu2", gpus=[gpu("0", 70.0, fan_speed_pct=None)])],
    )
    line = _find(render(config, snap), "gpu2")
    assert "fan --" in line
    assert "70°C" in line


def test_a_gpu_with_almost_no_metrics_still_renders(config):
    bare = gpu(
        "0",
        temperature_c=None,
        utilization_pct=None,
        fan_speed_pct=None,
        power_w=None,
        vram_used_bytes=None,
        vram_total_bytes=None,
    )
    line = _find(render(config, snapshot(NOW, machines=[machine("gpu2", gpus=[bare])])), "gpu2")
    assert line.count("--") >= 4


def test_partial_vram_renders_without_inventing_a_total(config):
    snap = snapshot(
        NOW,
        machines=[
            machine("gpu2", gpus=[gpu("0", 70.0, vram_total_bytes=None, vram_used_bytes=8.0 * 1024 ** 3)])
        ],
    )
    assert "8.0GB" in _find(render(config, snap), "gpu2")


# -- case 9: machine unavailable renders correctly ------------------------


def test_unavailable_machine_shows_unavailable_with_a_flag(config):
    snap = snapshot(
        NOW,
        machines=[
            machine("gpu2", gpus=[gpu("0", 60.0)]),
            machine("gpu3", available=False),
        ],
    )
    text = render(config, snap, assess(config, snap))
    line = _find(text, "gpu3")
    assert "unavailable (!)" in line
    assert "GPU" not in line


def test_available_machine_with_no_gpus_says_so(config):
    snap = snapshot(NOW, machines=[machine("gpu2", gpus=[])])
    assert "no GPUs reported" in _find(render(config, snap), "gpu2")


# -- sensor states (cases 2, 3) -------------------------------------------


def test_never_seen_sensor_reads_not_yet_seen(config_with_sensors):
    snap = snapshot(
        NOW,
        sensors=[
            sensor_ok("3201a", f_to_c(81.2)),
            sensor_state("3201b", SensorState.NEVER_SEEN),
            sensor_state("3201c", SensorState.NEVER_SEEN),
        ],
    )
    text = render(config_with_sensors, snap)
    assert "not yet seen" in _find(text, "BioE 3201B")
    assert "(!)" not in _find(text, "BioE 3201B")
    assert "81.2°F" in _find(text, "BioE 3201A")


def test_stale_sensor_shows_unavailable_and_never_its_last_value(config_with_sensors):
    snap = snapshot(
        NOW,
        sensors=[
            sensor_ok("3201a", f_to_c(79.0)),
            sensor_state("3201b", SensorState.STALE, last_seen=NOW - 5000),
            sensor_state("3201c", SensorState.NEVER_SEEN),
        ],
    )
    text = render(config_with_sensors, snap, assess(config_with_sensors, snap))
    line = _find(text, "BioE 3201B")
    assert "unavailable (!)" in line
    assert "°F" not in line


def test_pending_sensor_after_restart_reads_as_awaiting(config_with_sensors):
    snap = snapshot(NOW, sensors=[sensor_state("3201a", SensorState.PENDING)])
    line = _find(render(config_with_sensors, snap), "BioE 3201A")
    assert "awaiting reading" in line
    assert "(!)" not in line


def test_bluetooth_failure_is_reported_under_environment(config_with_sensors):
    snap = snapshot(
        NOW,
        sensors=[sensor_state("3201a", SensorState.NEVER_SEEN)],
        bluetooth_error="BleakError: no adapter",
    )
    text = render(config_with_sensors, snap)
    assert "Bluetooth: BleakError: no adapter" in text


def test_bluetooth_failure_is_reported_even_with_no_sensors(config):
    snap = snapshot(NOW, bluetooth_error="adapter down")
    assert "Bluetooth: adapter down" in render(config, snap)


def test_two_sensors_in_one_room_are_labelled_apart():
    config = make_config(
        sensors=[
            {"id": "a1", "name": "north", "room": "a", "address": "AA:00:00:00:00:01"},
            {"id": "a2", "name": "south", "room": "a", "address": "AA:00:00:00:00:02"},
        ]
    )
    snap = snapshot(
        NOW,
        sensors=[sensor_ok("a1", f_to_c(78.0)), sensor_ok("a2", f_to_c(80.0))],
    )
    text = render(config, snap)
    assert "BioE 3201A north" in text
    assert "BioE 3201A south" in text


# -- case 14: (!) marks only currently abnormal values --------------------


def test_flags_appear_only_on_abnormal_values(config_with_sensors):
    snap = snapshot(
        NOW,
        machines=[
            machine("gpu2", gpus=[gpu("0", 85.0)]),   # over the 80C GPU limit
            machine("gpu3", gpus=[gpu("0", 58.0)]),   # comfortable
        ],
        sensors=[
            sensor_ok("3201a", f_to_c(81.2)),        # under the 82F limit
            sensor_ok("3201b", f_to_c(84.7)),        # over it
            sensor_state("3201c", SensorState.NEVER_SEEN),
        ],
    )
    text = render(config_with_sensors, snap, assess(config_with_sensors, snap))

    assert "(!)" in _find(text, "BioE 3201B")
    assert "(!)" not in _find(text, "BioE 3201A")
    assert "(!)" not in _find(text, "BioE 3201C")
    assert "(!)" in _find(text, "gpu2")
    assert "(!)" not in _find(text, "gpu3")
    assert text.count("(!)") == 2


def test_high_utilization_and_power_are_never_flagged(config):
    """Busy GPUs are context for a hot room, not a fault in themselves."""
    snap = snapshot(
        NOW,
        machines=[
            machine(
                "gpu2",
                gpus=[gpu("0", 60.0, utilization_pct=100.0, power_w=450.0, fan_speed_pct=100.0)],
            )
        ],
    )
    text = render(config, snap, assess(config, snap))
    assert "100%" in text
    assert "450W" in text
    assert "(!)" not in text


def test_flag_uses_hysteresis_limit_while_alerting(config_with_sensors):
    """A room in alert at 81F is still abnormal: it has not cleared 80F yet."""
    engine = AlertEngine(config_with_sensors)
    hot = snapshot(NOW, sensors=[sensor_ok("3201b", f_to_c(85.0))])
    engine.evaluate(hot)
    engine.evaluate(snapshot(NOW + 700, sensors=[sensor_ok("3201b", f_to_c(85.0))]))

    drifting = snapshot(NOW + 800, sensors=[sensor_ok("3201b", f_to_c(81.0))])
    assessment = engine.evaluate(drifting)
    assert assessment.room_temperature_abnormal("b") is True
    assert "(!)" in _find(render(config_with_sensors, drifting, assessment), "BioE 3201B")


# -- message assembly -----------------------------------------------------


def test_message_wraps_the_dashboard_in_a_code_block(config, all_available):
    text = render_message(render(config, all_available(NOW)))
    assert text.startswith("```")
    assert text.rstrip().endswith("```")


def test_message_includes_headlines_above_the_dashboard(config, all_available):
    text = render_message(render(config, all_available(NOW)), [":warning: BioE 3201B is hot."])
    assert text.index(":warning: BioE 3201B is hot.") < text.index("```")


def test_message_appends_the_netdata_link_when_configured(all_available):
    config = make_config(env={"NETDATA_DASHBOARD_URL": "https://netdata.example"})
    text = render_message(
        render(config, all_available(NOW)), dashboard_url=config.netdata.dashboard_url
    )
    assert "<https://netdata.example|Full Netdata dashboard>" in text


def test_message_omits_the_link_when_not_configured(config, all_available):
    text = render_message(render(config, all_available(NOW)))
    assert "Netdata dashboard" not in text


# -- build_snapshot wiring ------------------------------------------------


class _FakeStore:
    """Stands in for the BLE store."""

    bluetooth_error = None

    def __init__(self, readings):
        self.readings = readings
        self.calls = []

    def reading(self, sensor, now, timeout_seconds):
        self.calls.append((sensor.id, now, timeout_seconds))
        return self.readings[sensor.id]


class _FakeNetdata:
    def __init__(self, readings):
        self.readings = readings


def test_build_snapshot_polls_every_machine_and_sensor(config_with_sensors, monkeypatch):
    import lab_monitor.status as status

    seen = []

    def fake_collect(client, machine_obj, now, timeout):
        seen.append((machine_obj.id, timeout))
        return machine(machine_obj.id, gpus=[gpu("0", 60.0)])

    monkeypatch.setattr(status, "collect_machine", fake_collect)
    store = _FakeStore({
        "3201a": sensor_ok("3201a", 25.0),
        "3201b": sensor_state("3201b", SensorState.NEVER_SEEN),
        "3201c": sensor_state("3201c", SensorState.NEVER_SEEN),
    })

    snap = build_snapshot(config_with_sensors, _FakeNetdata({}), store, NOW)

    assert [m.machine_id for m in snap.machines] == ["deepthought", "gpu2", "gpu3"]
    assert {mid for mid, _ in seen} == {"deepthought", "gpu2", "gpu3"}
    assert all(timeout == 180 for _, timeout in seen)
    assert [c[2] for c in store.calls] == [600, 600, 600]
    assert snap.taken_at == NOW


def _find(text, needle):
    """The single dashboard line containing ``needle``."""
    matches = [line for line in text.splitlines() if needle in line]
    assert matches, "no line containing %r in:\n%s" % (needle, text)
    return matches[0]
