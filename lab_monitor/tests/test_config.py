"""Configuration loading, validation and environment resolution."""

from __future__ import annotations

import copy

import pytest
from conftest import BASE_CONFIG, make_config

from lab_monitor.config import ConfigError, load_config, parse_config


def test_parses_the_example_topology(config):
    assert config.site_name == "BioE 3201"
    assert [room.id for room in config.rooms] == ["a", "b", "c", "d", "foyer"]
    assert config.room("foyer").name == "Foyer"
    assert config.machine("gpu2").netdata_hostname == "gpu2"
    assert config.machine("deepthought").parent is True


def test_deepthought_is_listed_like_any_other_machine(config):
    """The parent is monitored too, not just used as a data source."""
    assert config.machine("deepthought") is not None
    assert config.machines_in("foyer") == (config.machine("deepthought"),)


def test_rooms_may_hold_several_machines():
    config = make_config(
        machines=BASE_CONFIG["machines"] + [
            {"id": "gpu4", "name": "gpu4", "room": "a", "netdata_hostname": "gpu4"}
        ]
    )
    assert [m.id for m in config.machines_in("a")] == ["gpu2", "gpu4"]


def test_zero_sensors_is_valid(config):
    assert config.sensors == ()


def test_sensor_without_address_is_allowed_and_warns():
    config = make_config(
        sensors=[{"id": "3201a", "name": "BioE 3201A sensor", "room": "a"}]
    )
    assert config.sensors[0].address is None
    assert any("no BLE address" in w for w in config.warnings)


def test_room_order_controls_display_and_tolerates_omissions():
    config = make_config(display={"room_order": ["foyer", "a"]})
    assert [room.id for room in config.ordered_rooms()] == ["foyer", "a", "b", "c", "d"]


def test_machine_address_resolves_from_the_environment():
    raw = copy.deepcopy(BASE_CONFIG)
    raw["machines"][1]["address_env"] = "GPU2_IP"
    config = parse_config(raw, env={"GPU2_IP": "10.0.0.2"})
    assert config.machine_addresses["gpu2"] == "10.0.0.2"
    assert config.warnings == ()


def test_missing_address_env_warns_but_does_not_fail():
    raw = copy.deepcopy(BASE_CONFIG)
    raw["machines"][1]["address_env"] = "GPU2_IP"
    config = parse_config(raw, env={})
    assert "gpu2" not in config.machine_addresses
    assert any("GPU2_IP" in w for w in config.warnings)


def test_netdata_url_and_dashboard_come_from_the_environment():
    config = make_config(
        env={
            "NETDATA_URL": "http://10.0.0.1:19999/",
            "NETDATA_DASHBOARD_URL": "https://netdata.example/",
        }
    )
    assert config.netdata.url == "http://10.0.0.1:19999"
    assert config.netdata.dashboard_url == "https://netdata.example/"


def test_slack_credentials_come_from_the_environment_only():
    config = make_config(
        env={
            "LAB_MONITOR_SLACK_BOT_TOKEN": "xoxb-x",
            "LAB_MONITOR_SLACK_APP_TOKEN": "xapp-x",
            "LAB_MONITOR_SLACK_CHANNEL_ID": "C123",
        }
    )
    assert config.slack.configured is True
    assert config.slack.channel_id == "C123"


def test_slack_is_optional():
    assert make_config().slack.configured is False


def test_thresholds_compare_celsius_readings_against_a_fahrenheit_limit(config):
    threshold = config.threshold("room_temperature")
    assert threshold.high == 82.0
    assert threshold.is_abnormal_c(28.0, alerting=False) is True     # 82.4F
    assert threshold.is_abnormal_c(27.0, alerting=False) is False    # 80.6F


def test_hysteresis_limit_depends_on_current_state(config):
    threshold = config.threshold("room_temperature")
    assert threshold.limit(alerting=False) == 82.0
    assert threshold.limit(alerting=True) == 80.0


# -- case 17: malformed configuration produces a useful error -------------


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda raw: raw.update(rooms=[]), "at least one [[rooms]]"),
        (lambda raw: raw.update(machines=[]), "at least one [[machines]]"),
        (
            lambda raw: raw["machines"][1].update(room="nowhere"),
            "does not match any [[rooms]] id",
        ),
        (
            lambda raw: raw["rooms"].append({"id": "a", "name": "Duplicate"}),
            "duplicate room id",
        ),
        (
            lambda raw: raw["machines"].append(
                {"id": "gpu2", "name": "x", "room": "a", "netdata_hostname": "x"}
            ),
            "duplicate machine id",
        ),
        (
            lambda raw: raw["machines"][1].update(parent=True),
            "only one machine may set parent",
        ),
        (lambda raw: raw["machines"][1].pop("id"), "machines[1].id is required"),
        (
            lambda raw: raw["thresholds"]["room_temperature"].update(unit="kelvin"),
            'thresholds.room_temperature.unit must be "C" or "F"',
        ),
        (
            lambda raw: raw["thresholds"]["room_temperature"].pop("high"),
            "thresholds.room_temperature.high is required",
        ),
        (
            lambda raw: raw["thresholds"].update(gpu_temp={"high": 1}),
            "unknown threshold section(s): gpu_temp",
        ),
        (
            lambda raw: raw["thresholds"]["room_temperature"].update(recovery_margin=-1),
            "recovery_margin must not be negative",
        ),
        (
            lambda raw: raw["display"].update(room_order=["a", "nope"]),
            "unknown room id(s): nope",
        ),
        (
            lambda raw: raw["availability"].update(machine_timeout_seconds=-5),
            "must be a non-negative integer",
        ),
        (
            lambda raw: raw.update(sensors=[{"id": "s1", "room": "gone"}]),
            "does not match any [[rooms]] id",
        ),
    ],
)
def test_malformed_config_names_the_problem(mutate, expected):
    raw = copy.deepcopy(BASE_CONFIG)
    mutate(raw)
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, env={})
    assert expected in str(excinfo.value)


def test_missing_config_file_explains_how_to_fix_it(tmp_path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(missing), env={})
    message = str(excinfo.value)
    assert "config file not found" in message
    assert "LAB_MONITOR_CONFIG" in message


def test_unparseable_toml_names_the_file(tmp_path):
    broken = tmp_path / "broken.toml"
    broken.write_text("[site\nname = ", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(broken), env={})
    assert "could not parse" in str(excinfo.value)
    assert "broken.toml" in str(excinfo.value)


def test_shipped_example_config_is_valid(tmp_path):
    """The file we tell people to copy must actually load."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    config = load_config(str(example), env={})
    assert config.site_name == "BioE 3201"
    assert len(config.machines) == 3
    assert config.sensors == ()
    assert config.threshold("room_temperature").high == 82.0
