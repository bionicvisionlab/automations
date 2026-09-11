"""The raw per-poll telemetry CSV.

Covers what a year-old file has to survive: blanks that mean "unknown" rather
than zero, timestamps that are still unambiguous after a DST move, and a schema
change that starts a new file instead of misaligning every row after it.
"""

from __future__ import annotations

import csv
import datetime

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


def healthy(now=NOW):
    """One OK sensor and one available single-GPU machine."""
    return snapshot(
        now,
        machines=[machine("gpu2", gpus=[gpu("0", 61.5)])],
        sensors=[sensor_ok("3201a", 23.5, humidity_pct=41.0)],
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


def test_a_stale_sensor_writes_blanks_rather_than_its_last_reading(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(healthy())
    telemetry.append(
        snapshot(
            NOW + 30,
            machines=[machine("gpu2", gpus=[gpu("0", 61.5)])],
            sensors=[sensor_state("3201a", SensorState.STALE, last_seen=NOW)],
        )
    )

    assert cell(telemetry.path, "sensor.3201a.temperature_c", row=1) == "23.5"
    assert cell(telemetry.path, "sensor.3201a.temperature_c", row=2) == ""
    assert cell(telemetry.path, "sensor.3201a.humidity_pct", row=2) == ""


def test_a_never_seen_or_pending_sensor_writes_blanks(tmp_path):
    telemetry = log(tmp_path)
    for state in (SensorState.NEVER_SEEN, SensorState.PENDING):
        telemetry.append(snapshot(NOW, sensors=[sensor_state("3201a", state)]))

    for row in (1, 2):
        assert cell(telemetry.path, "sensor.3201a.temperature_c", row=row) == ""


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


def test_a_machine_with_no_sensor_reading_at_all_still_produces_a_full_row(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(snapshot(NOW, machines=[machine("gpu2", gpus=[gpu("0")])]))

    table = rows(telemetry.path)
    assert len(table[1]) == len(table[0])
    assert cell(telemetry.path, "sensor.3201a.temperature_c") == ""


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


def test_a_header_from_a_different_configuration_starts_a_new_file(tmp_path):
    path = tmp_path / "lab_monitor.csv"
    path.write_text("timestamp,sensor.old.temperature_c\n2026-01-01T00:00:00+00:00,21\n")

    telemetry = log(tmp_path)
    telemetry.append(healthy())

    # The old file is untouched, not appended to with a foreign row width.
    assert rows(path) == [
        ["timestamp", "sensor.old.temperature_c"],
        ["2026-01-01T00:00:00+00:00", "21"],
    ]
    assert str(telemetry.path) != str(path)
    assert telemetry.path.endswith(".csv")
    assert "lab_monitor-" in telemetry.path

    table = rows(telemetry.path)
    assert len(table) == 2
    assert "sensor.3201a.temperature_c" in table[0]
    assert "sensor.old.temperature_c" not in table[0]


def test_a_new_gpu_appearing_mid_run_starts_a_new_file(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(healthy())
    first = telemetry.path

    telemetry.append(
        snapshot(NOW + 30, machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])])
    )

    assert telemetry.path != first
    assert len(rows(first)) == 2                       # the single-GPU rows stay put
    assert "gpu.gpu2.1.temperature_c" in rows(telemetry.path)[0]
    assert len(rows(telemetry.path)) == 2

    # And the wider schema is then appended to, not rotated again.
    telemetry.append(
        snapshot(NOW + 60, machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])])
    )
    assert len(rows(telemetry.path)) == 3


def test_a_second_gpu_is_remembered_across_a_poll_that_loses_it(tmp_path):
    telemetry = log(tmp_path)
    telemetry.append(snapshot(NOW, machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])]))
    path = telemetry.path

    telemetry.append(snapshot(NOW + 30, machines=[machine("gpu2", gpus=[gpu("0")])]))

    assert telemetry.path == path
    assert cell(path, "gpu.gpu2.1.temperature_c", row=2) == ""


def test_a_restart_adopts_the_gpu_columns_the_file_already_has(tmp_path):
    first = log(tmp_path)
    first.append(snapshot(NOW, machines=[machine("gpu2", gpus=[gpu("0"), gpu("1")])]))

    # Comes back up while the machine is offline, so the snapshot shows no GPUs.
    second = log(tmp_path)
    second.append(snapshot(NOW + 30, machines=[machine("gpu2", available=False)]))

    assert second.path == first.path
    assert len(rows(first.path)) == 3


def test_two_rotations_in_the_same_second_do_not_overwrite_each_other(tmp_path):
    (tmp_path / "lab_monitor.csv").write_text("timestamp,stale\n")
    log(tmp_path).append(healthy())

    (tmp_path / "lab_monitor.csv").write_text("timestamp,stale\n")
    second = log(tmp_path)
    second.append(healthy())

    stamped = sorted(p.name for p in tmp_path.glob("lab_monitor-*.csv"))
    assert len(stamped) == 2
    assert len(rows(second.path)) == 2


def test_an_empty_file_is_given_a_header_rather_than_replaced(tmp_path):
    path = tmp_path / "lab_monitor.csv"
    path.touch()

    telemetry = log(tmp_path)
    telemetry.append(healthy())

    assert str(telemetry.path) == str(path)
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


def test_a_row_left_unterminated_by_a_crash_is_closed_off_not_spliced(tmp_path):
    path = tmp_path / "lab_monitor.csv"
    complete = log(tmp_path)
    complete.append(healthy())
    path.write_bytes(path.read_bytes().rstrip(b"\r\n"))      # as a kill would leave it

    telemetry = log(tmp_path)
    telemetry.append(healthy(NOW + 30))

    assert telemetry.path == complete.path
    table = rows(path)
    assert len(table) == 3
    assert len({len(row) for row in table}) == 1
