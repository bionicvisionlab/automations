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


def test_machines_carry_no_addresses():
    """LabMonitor reaches every machine through the Parent, so it never needs
    their IPs; those belong to each machine's Netdata stream.conf."""
    config = make_config()
    assert not hasattr(config.machines[0], "address_env")
    assert not hasattr(config, "machine_addresses")


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
            "LAB_MONITOR_SLACK_ROOM_CHANNEL_ID": "CROOM",
            "LAB_MONITOR_SLACK_COMPUTE_CHANNEL_ID": "CGPU",
        }
    )
    assert config.slack.configured is True
    assert config.slack.room_channel_id == "CROOM"
    assert config.slack.compute_channel_id == "CGPU"


def test_the_legacy_channel_feeds_both_notification_channels():
    config = make_config(env={"LAB_MONITOR_SLACK_CHANNEL_ID": "C123"})
    assert config.slack.room_channel_id == "C123"
    assert config.slack.compute_channel_id == "C123"


def test_a_domain_channel_overrides_the_legacy_one_for_that_domain_only():
    config = make_config(
        env={
            "LAB_MONITOR_SLACK_CHANNEL_ID": "C123",
            "LAB_MONITOR_SLACK_COMPUTE_CHANNEL_ID": "CGPU",
        }
    )
    assert config.slack.room_channel_id == "C123"
    assert config.slack.compute_channel_id == "CGPU"


def test_the_slack_app_needs_both_tokens_but_no_channel():
    tokens = {"LAB_MONITOR_SLACK_BOT_TOKEN": "xoxb-x", "LAB_MONITOR_SLACK_APP_TOKEN": "xapp-x"}
    assert make_config(env=tokens).slack.configured is True
    assert make_config(
        env={"LAB_MONITOR_SLACK_BOT_TOKEN": "xoxb-x", "LAB_MONITOR_SLACK_CHANNEL_ID": "C123"}
    ).slack.configured is False


def test_slack_is_optional():
    slack = make_config().slack
    assert slack.configured is False
    assert slack.room_channel_id is None
    assert slack.compute_channel_id is None


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
    assert config.weather.enabled is True
    assert (config.weather.latitude, config.weather.longitude) == (34.41305, -119.84487)


# -- booleans must be real TOML booleans -----------------------------------


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda raw: raw["machines"][1].update(parent="yes"), "machines[1].parent must be true or false"),
        (
            lambda raw: raw["thresholds"]["room_temperature"].update(enabled="false"),
            "thresholds.room_temperature.enabled must be true or false",
        ),
        (
            lambda raw: raw["availability"].update(alert_on_machine_unavailable="no"),
            "availability.alert_on_machine_unavailable must be true or false",
        ),
        (
            lambda raw: raw["availability"].update(alert_on_sensor_unavailable=0),
            "availability.alert_on_sensor_unavailable must be true or false",
        ),
        (
            lambda raw: raw.update(netdata={"statsd": {"enabled": "true"}}),
            "netdata.statsd.enabled must be true or false",
        ),
    ],
)
def test_a_stringy_boolean_is_rejected_not_coerced(mutate, expected):
    """`enabled = "false"` is truthy to bool(); it must be an error instead."""
    raw = copy.deepcopy(BASE_CONFIG)
    mutate(raw)
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, env={})
    assert expected in str(excinfo.value)


def test_real_booleans_are_accepted():
    raw = copy.deepcopy(BASE_CONFIG)
    raw["thresholds"]["room_temperature"]["enabled"] = False
    raw["availability"]["alert_on_sensor_unavailable"] = True
    config = parse_config(raw, env={})
    assert config.threshold("room_temperature").enabled is False
    assert config.availability.alert_on_sensor_unavailable is True


def test_sensor_availability_alerts_are_off_unless_asked_for():
    """Machines still announce themselves; chatty BLE sensors do not."""
    config = parse_config(copy.deepcopy(BASE_CONFIG), env={})
    assert config.availability.alert_on_sensor_unavailable is False
    assert config.availability.alert_on_machine_unavailable is True


# -- telemetry logging -----------------------------------------------------


def test_telemetry_logging_is_off_unless_a_path_is_given():
    config = make_config()
    assert config.logging.path is None
    assert config.logging.enabled is False


def test_a_logging_path_turns_telemetry_on():
    config = make_config(logging={"path": "/var/lib/bvl-automations/lab_monitor.csv"})
    assert config.logging.path == "/var/lib/bvl-automations/lab_monitor.csv"
    assert config.logging.enabled is True


@pytest.mark.parametrize("path", ["", 42, True])
def test_a_logging_path_that_is_not_a_usable_string_is_rejected(path):
    with pytest.raises(ConfigError) as excinfo:
        make_config(logging={"path": path})
    assert "logging.path must be a non-empty string" in str(excinfo.value)


# -- weather advisory ------------------------------------------------------


BEE = {"enabled": True, "latitude": 34.41305, "longitude": -119.84487, "notify_high": 82}


def test_the_weather_advisory_is_off_unless_enabled():
    config = make_config()
    assert config.weather.enabled is False
    assert config.weather.unit == "F"


def test_an_enabled_weather_advisory_carries_its_location_and_limit():
    config = make_config(weather=dict(BEE, unit="c"))
    assert config.weather.enabled is True
    assert (config.weather.latitude, config.weather.longitude) == (34.41305, -119.84487)
    assert config.weather.notify_high == 82.0
    assert config.weather.unit == "C"


def test_a_disabled_weather_section_needs_no_location():
    assert make_config(weather={"enabled": False}).weather.enabled is False


@pytest.mark.parametrize("missing", ["latitude", "longitude", "notify_high"])
def test_an_enabled_weather_advisory_requires_its_settings(missing):
    weather = dict(BEE)
    weather.pop(missing)
    with pytest.raises(ConfigError) as excinfo:
        make_config(weather=weather)
    assert "weather.%s is required when weather.enabled = true" % missing in str(excinfo.value)


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"latitude": 91}, "weather.latitude must be between -90 and 90"),
        ({"longitude": -181}, "weather.longitude must be between -180 and 180"),
        ({"notify_high": "hot"}, "weather.notify_high must be a number"),
        ({"unit": "kelvin"}, 'weather.unit must be "C" or "F"'),
        ({"enabled": "true"}, "weather.enabled must be true or false"),
    ],
)
def test_malformed_weather_settings_name_the_problem(override, expected):
    with pytest.raises(ConfigError) as excinfo:
        make_config(weather=dict(BEE, **override))
    assert expected in str(excinfo.value)
