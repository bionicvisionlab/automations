"""The raw per-poll telemetry CSV.

Covers what a year-old file has to survive: blanks that mean "unknown" rather
than zero, one broadcast counted once rather than twenty times, timestamps that
stay unambiguous after a DST move, and a schema change that archives the old
file instead of misaligning every row after it.
"""

from __future__ import annotations

import csv
import datetime

import pytest
from conftest import gpu, machine, sensor_ok, sensor_state, snapshot

from lab_monitor.csvlog import CsvLog
from lab_monitor.models import SensorState

NOW = 1_700_000_000.0

GIB = 1024.0 ** 3


def log(tmp_path, name="lab_monitor.csv", sensors=("3201a",), machines=("gpu2",)):
    """A CsvLog over a temp file, with the usual one sensor and one machine."""
    return CsvLog(tmp_path / name, sensor_ids=sensors, machine_ids=machines)


def rows(path):
    """Every row of a CSV, header first."""
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def cell(path, column, row=1):
    """One named cell, by header lookup rather than position."""
    table = rows(path)
    return table[row][table[0].index(column)]


def healthy(now=NOW, last_seen=None):
    """One OK sensor and one available single-GPU machine."""
    return snapshot(
        now,
        machines=[machine("gpu2", gpus=[gpu("0", 61.5)])],
        sensors=[
            sensor_ok("3201a", 23.5, humidity_pct=41.0, last_seen=now if last_seen is None else last_seen)
        ],
    )


# -- first write -----------------------------------------------------------


def test_the_first_append_creates_the_header_and_one_row(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(healthy())

    table = rows(telemetry.path)
    assert len(table) == 2
    assert table[0][0] == "timestamp"
    assert "sensor.3201a.temperature_c" in table[0]
    assert "gpu.gpu2.0.temperature_c" in table[0]
    assert len(table[1]) == len(table[0])


def test_every_configured_sensor_and_discovered_gpu_gets_its_columns(tmp_path):
    telemetry = CsvLog(
        tmp_path / "t.csv", sensor_ids=["3201a", "3201b"], machine_ids=["gpu2", "gpu3"]
    )
    telemetry.append(
        snapshot(
            NOW,
            machines=[
                machine("gpu2", gpus=[gpu("0"), gpu("1")]),
                machine("gpu3", gpus=[gpu("0")]),
            ],
            sensors=[sensor_ok("3201a", 23.0), sensor_ok("3201b", 24.0)],
        )
    )

    header = rows(telemetry.path)[0]
    # 1 timestamp + 2 sensors x 3 fields + 3 GPUs x 6 fields
    assert len(header) == 1 + 6 + 18
    for name in ("gpu.gpu2.0.power_w", "gpu.gpu2.1.power_w", "gpu.gpu3.0.power_w"):
        assert name in header
    # Declaration order, so a file's columns read like the configuration.
    assert header.index("sensor.3201a.temperature_c") < header.index(
        "sensor.3201b.temperature_c"
    )
    assert header.index("gpu.gpu2.1.temperature_c") < header.index(
        "gpu.gpu3.0.temperature_c"
    )


def test_measurements_are_written_as_plain_numbers(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(
        snapshot(
            NOW,
            machines=[
                machine("gpu2", gpus=[gpu("0", 61.5, power_w=382.0, vram_used_bytes=18.0 * GIB)])
            ],
            sensors=[sensor_ok("3201a", 23.5, humidity_pct=41.0)],
        )
    )

    assert cell(telemetry.path, "sensor.3201a.temperature_c") == "23.5"
    assert cell(telemetry.path, "sensor.3201a.humidity_pct") == "41"
    assert cell(telemetry.path, "gpu.gpu2.0.temperature_c") == "61.5"
    assert cell(telemetry.path, "gpu.gpu2.0.power_w") == "382"
    # Bytes, not gigabytes: the log stores what was measured, not what is shown.
    assert cell(telemetry.path, "gpu.gpu2.0.vram_used_bytes") == "19327352832"


# -- appending -------------------------------------------------------------


def test_later_polls_append_rows_under_the_same_header(tmp_path):
    telemetry = log(tmp_path)
    for offset in range(3):
        telemetry.append(healthy(NOW + 30 * offset))

    table = rows(telemetry.path)
    assert len(table) == 4
    assert len({len(row) for row in table}) == 1
    stamps = [row[0] for row in table[1:]]
    assert stamps == sorted(stamps) and len(set(stamps)) == 3


def test_a_restart_keeps_appending_to_the_same_file(tmp_path):
    first = log(tmp_path)
    first.append(healthy())

    second = log(tmp_path)
    second.append(healthy(NOW + 30))

    assert second.path == first.path
    assert len(rows(first.path)) == 3


# -- blanks ----------------------------------------------------------------


@pytest.mark.parametrize(
    "later",
    [
        pytest.param(sensor_state("3201a", SensorState.STALE, last_seen=NOW), id="stale"),
        pytest.param(sensor_state("3201a", SensorState.NEVER_SEEN), id="never_seen"),
        pytest.param(sensor_state("3201a", SensorState.PENDING), id="pending"),
        pytest.param(None, id="absent_from_snapshot"),
    ],
)
def test_a_sensor_without_a_current_value_writes_blanks_not_its_last_reading(tmp_path, later):
    telemetry = log(tmp_path)
    telemetry.append(healthy())
    telemetry.append(
        snapshot(
            NOW + 30,
            machines=[machine("gpu2", gpus=[gpu("0", 61.5)])],
            sensors=[] if later is None else [later],
        )
    )

    assert cell(telemetry.path, "sensor.3201a.temperature_c", row=1) == "23.5"
    for field in ("temperature_c", "humidity_pct", "battery_pct"):
        assert cell(telemetry.path, "sensor.3201a.%s" % field, row=2) == ""


def test_one_broadcast_is_logged_once_not_on_every_poll_until_it_goes_stale(tmp_path):
    """``SensorStore`` serves the same reading as OK for the whole staleness window."""
    telemetry = log(tmp_path)
    for offset in (0, 30, 60):
        # Same last_seen: the sensor has not broadcast again since NOW.
        telemetry.append(healthy(NOW + offset, last_seen=NOW))

    assert cell(telemetry.path, "sensor.3201a.temperature_c", row=1) == "23.5"
    for row in (2, 3):
        for field in ("temperature_c", "humidity_pct", "battery_pct"):
            assert cell(telemetry.path, "sensor.3201a.%s" % field, row=row) == ""

    # GPU cells are polled values, so they keep being written.
    assert cell(telemetry.path, "gpu.gpu2.0.temperature_c", row=3) == "61.5"


def test_a_fresh_broadcast_is_logged_again(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(healthy(NOW, last_seen=NOW))
    telemetry.append(healthy(NOW + 30, last_seen=NOW))
    telemetry.append(
        snapshot(
            NOW + 60,
            sensors=[sensor_ok("3201a", 24.0, humidity_pct=42.0, last_seen=NOW + 55)],
        )
    )

    assert cell(telemetry.path, "sensor.3201a.temperature_c", row=1) == "23.5"
    assert cell(telemetry.path, "sensor.3201a.temperature_c", row=2) == ""
    assert cell(telemetry.path, "sensor.3201a.temperature_c", row=3) == "24"


def test_a_restart_logs_the_current_reading_once_more(tmp_path):
    """The in-memory record of what was logged does not survive; one repeat is fine."""
    first = log(tmp_path)
    first.append(healthy(NOW, last_seen=NOW))
    first.append(healthy(NOW + 30, last_seen=NOW))

    second = log(tmp_path)
    second.append(healthy(NOW + 60, last_seen=NOW))
    second.append(healthy(NOW + 90, last_seen=NOW))

    path = first.path
    assert cell(path, "sensor.3201a.temperature_c", row=1) == "23.5"
    assert cell(path, "sensor.3201a.temperature_c", row=2) == ""
    assert cell(path, "sensor.3201a.temperature_c", row=3) == "23.5"
    assert cell(path, "sensor.3201a.temperature_c", row=4) == ""


def test_an_unavailable_machine_writes_blanks_under_its_existing_gpu_columns(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(healthy())
    path = telemetry.path

    telemetry.append(snapshot(NOW + 30, machines=[machine("gpu2", available=False)]))

    # Same file: a machine being down is not a schema change.
    assert telemetry.path == path
    assert len(rows(path)) == 3
    for field in ("temperature_c", "utilization_pct", "vram_total_bytes"):
        assert cell(path, "gpu.gpu2.0.%s" % field, row=2) == ""


def test_a_metric_the_driver_did_not_report_is_blank_while_its_neighbours_are_not(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(
        snapshot(
            NOW,
            machines=[machine("gpu2", gpus=[gpu("0", 61.5, fan_speed_pct=None, power_w=None)])],
        )
    )

    assert cell(telemetry.path, "gpu.gpu2.0.fan_speed_pct") == ""
    assert cell(telemetry.path, "gpu.gpu2.0.power_w") == ""
    assert cell(telemetry.path, "gpu.gpu2.0.temperature_c") == "61.5"


def test_a_zero_reading_is_a_zero_and_not_a_blank(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(
        snapshot(NOW, machines=[machine("gpu2", gpus=[gpu("0", 40.0, utilization_pct=0.0)])])
    )

    assert cell(telemetry.path, "gpu.gpu2.0.utilization_pct") == "0"


# -- timestamps ------------------------------------------------------------


def test_the_timestamp_is_timezone_aware_iso_8601(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(healthy())

    stamp = rows(telemetry.path)[1][0]
    parsed = datetime.datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None
    assert parsed.timestamp() == NOW


def test_the_timestamp_is_the_snapshot_time_not_the_write_time(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(healthy(NOW - 3600))

    parsed = datetime.datetime.fromisoformat(rows(telemetry.path)[1][0])
    assert parsed.timestamp() == NOW - 3600


# -- schema changes --------------------------------------------------------


def archived(tmp_path):
    """The timestamp-suffixed files left behind by schema changes."""
    return sorted(p.name for p in tmp_path.glob("lab_monitor-*.csv"))


def test_a_header_from_a_different_configuration_is_archived_not_appended_to(tmp_path):
    path = tmp_path / "lab_monitor.csv"
    path.write_text("timestamp,sensor.old.temperature_c\n2026-01-01T00:00:00+00:00,21\n")

    telemetry = log(tmp_path)
    telemetry.append(healthy())

    # The configured path is the current schema; the old rows moved aside intact.
    assert telemetry.path == str(path)
    assert "sensor.3201a.temperature_c" in rows(path)[0]
    assert "sensor.old.temperature_c" not in rows(path)[0]
    assert len(rows(path)) == 2

    assert len(archived(tmp_path)) == 1
    assert rows(tmp_path / archived(tmp_path)[0]) == [
        ["timestamp", "sensor.old.temperature_c"],
        ["2026-01-01T00:00:00+00:00", "21"],
    ]


def test_a_new_gpu_appearing_mid_run_archives_the_narrower_file(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(healthy())
    path = telemetry.path

    telemetry.append(
        snapshot(NOW + 30, machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])])
    )

    assert telemetry.path == path
    assert "gpu.gpu2.1.temperature_c" in rows(path)[0]
    assert len(rows(path)) == 2
    assert "gpu.gpu2.1.temperature_c" not in rows(tmp_path / archived(tmp_path)[0])[0]

    # And the wider schema is then appended to, not archived again.
    telemetry.append(
        snapshot(NOW + 60, machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])])
    )
    assert len(rows(path)) == 3
    assert len(archived(tmp_path)) == 1


def test_a_restart_after_a_schema_change_resumes_the_current_file(tmp_path):
    """The regression: the configured path must not still hold the old schema."""
    first = log(tmp_path)
    first.append(healthy())
    first.append(snapshot(NOW + 30, machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])]))
    assert len(archived(tmp_path)) == 1

    for restart in range(3):
        resumed = log(tmp_path)
        resumed.append(
            snapshot(
                NOW + 60 + 30 * restart,
                machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])],
            )
        )
        assert resumed.path == first.path

    # One archive from the real change, none from the restarts.
    assert len(archived(tmp_path)) == 1
    assert len(rows(first.path)) == 5


def test_a_second_gpu_is_remembered_across_a_poll_that_loses_it(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(snapshot(NOW, machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])]))

    telemetry.append(snapshot(NOW + 30, machines=[machine("gpu2", gpus=[gpu("0")])]))

    assert archived(tmp_path) == []
    assert cell(telemetry.path, "gpu.gpu2.1.temperature_c", row=2) == ""


def test_a_restart_adopts_the_gpu_columns_the_file_already_has(tmp_path):
    first = log(tmp_path)
    first.append(snapshot(NOW, machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])]))

    # Comes back up while the machine is offline, so the snapshot shows no GPUs.
    second = log(tmp_path)
    second.append(snapshot(NOW + 30, machines=[machine("gpu2", available=False)]))

    assert second.path == first.path
    assert archived(tmp_path) == []
    assert len(rows(first.path)) == 3


def test_two_archives_in_the_same_second_do_not_overwrite_each_other(tmp_path):
    for _ in range(2):
        (tmp_path / "lab_monitor.csv").write_text("timestamp,stale\n")
        telemetry = log(tmp_path)
        telemetry.append(healthy())

    assert len(archived(tmp_path)) == 2
    assert len(rows(tmp_path / "lab_monitor.csv")) == 2


def test_an_empty_file_is_given_a_header_rather_than_archived(tmp_path):
    path = tmp_path / "lab_monitor.csv"
    path.touch()

    telemetry = log(tmp_path)
    telemetry.append(healthy())

    assert telemetry.path == str(path)
    assert archived(tmp_path) == []
    assert len(rows(path)) == 2


def test_gpu_indexes_are_ordered_numerically(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(
        snapshot(
            NOW,
            machines=[machine("gpu2", gpus=[gpu("10"), gpu("2"), gpu("0")])],
        )
    )

    header = rows(telemetry.path)[0]
    positions = [header.index("gpu.gpu2.%s.temperature_c" % i) for i in ("0", "2", "10")]
    assert positions == sorted(positions)
