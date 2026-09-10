"""Threshold state machine, debounce, hysteresis and persistence."""

from __future__ import annotations

import json

import pytest
from conftest import f_to_c, gpu, machine, make_config, sensor_ok, sensor_state, snapshot

from lab_monitor.alerts import (
    AlertEngine,
    load_state,
    machine_key,
    room_key,
    save_state,
    sensor_key,
)
from lab_monitor.models import SensorState, TransitionKind

NOW = 1_700_000_000.0

HOT = f_to_c(85.0)      # over the 82F room limit
WARM = f_to_c(81.5)     # under 82F, but over the 80F recovery limit
COOL = f_to_c(75.0)     # comfortably normal


def sensors_config():
    return make_config(
        sensors=[
            {"id": "3201a", "name": "A", "room": "a", "address": "AA:01"},
            {"id": "3201b", "name": "B", "room": "b", "address": "AA:02"},
        ]
    )


def room_snapshot(now, temperature_c, sensor_id="3201b"):
    return snapshot(now, sensors=[sensor_ok(sensor_id, temperature_c)])


def kinds(assessment):
    return [t.kind for t in assessment.transitions]


def gpu_keys(assessment):
    """Only the GPU-temperature transitions, ignoring availability noise."""
    return [t.key for t in assessment.transitions if t.key.startswith("gpu_temperature:")]


def room_keys(assessment):
    """Only the room-temperature transitions."""
    return [t.key for t in assessment.transitions if t.key.startswith("room_temperature:")]


# -- case 1: zero sensors configured --------------------------------------


def test_no_sensors_means_no_environment_conditions(config, all_available):
    engine = AlertEngine(config)
    assessment = engine.evaluate(all_available(NOW))
    assert assessment.transitions == ()
    assert assessment.abnormal_keys == frozenset()


def test_service_stays_quiet_across_many_polls_with_no_sensors(config, all_available):
    engine = AlertEngine(config)
    for tick in range(20):
        assert engine.evaluate(all_available(NOW + tick * 30)).transitions == ()


# -- case 2: configured sensor never seen ---------------------------------


def test_never_seen_sensor_never_alerts():
    config = sensors_config()
    engine = AlertEngine(config)
    for tick in range(50):
        snap = snapshot(
            NOW + tick * 60,
            sensors=[
                sensor_state("3201a", SensorState.NEVER_SEEN),
                sensor_state("3201b", SensorState.NEVER_SEEN),
            ],
        )
        assert engine.evaluate(snap).transitions == ()


# -- cases 3-5: sensor goes stale, stays stale, comes back ----------------


def test_established_sensor_going_stale_alerts_exactly_once():
    config = sensors_config()
    engine = AlertEngine(config)

    engine.evaluate(snapshot(NOW, sensors=[sensor_ok("3201b", COOL)]))

    stale = snapshot(NOW + 700, sensors=[sensor_state("3201b", SensorState.STALE)])
    first = engine.evaluate(stale)
    assert kinds(first) == [TransitionKind.ALERT]
    assert "BioE 3201B sensor is unavailable" in first.transitions[0].headline
    assert first.sensor_unavailable("3201b") is True


def test_a_stale_sensor_that_stays_stale_says_nothing_more():
    config = sensors_config()
    engine = AlertEngine(config)
    engine.evaluate(snapshot(NOW, sensors=[sensor_ok("3201b", COOL)]))
    engine.evaluate(snapshot(NOW + 700, sensors=[sensor_state("3201b", SensorState.STALE)]))

    for tick in range(1, 40):
        snap = snapshot(NOW + 700 + tick * 60, sensors=[sensor_state("3201b", SensorState.STALE)])
        assert engine.evaluate(snap).transitions == ()


def test_a_returning_sensor_produces_one_recovery():
    config = sensors_config()
    engine = AlertEngine(config)
    engine.evaluate(snapshot(NOW, sensors=[sensor_ok("3201b", COOL)]))
    engine.evaluate(snapshot(NOW + 700, sensors=[sensor_state("3201b", SensorState.STALE)]))

    back = engine.evaluate(snapshot(NOW + 1400, sensors=[sensor_ok("3201b", COOL)]))
    assert kinds(back) == [TransitionKind.RECOVERY]
    assert "reporting again" in back.transitions[0].headline

    later = engine.evaluate(snapshot(NOW + 1500, sensors=[sensor_ok("3201b", COOL)]))
    assert later.transitions == ()


def test_sensor_availability_alerts_can_be_switched_off():
    config = make_config(
        sensors=[{"id": "3201b", "name": "B", "room": "b", "address": "AA:02"}],
        availability={"alert_on_sensor_unavailable": False, "sensor_timeout_seconds": 600},
    )
    engine = AlertEngine(config)
    engine.evaluate(snapshot(NOW, sensors=[sensor_ok("3201b", COOL)]))
    assessment = engine.evaluate(
        snapshot(NOW + 700, sensors=[sensor_state("3201b", SensorState.STALE)])
    )
    assert assessment.transitions == ()
    # Still shown as abnormal on the dashboard, just not announced.
    assert assessment.sensor_unavailable("3201b") is True


def test_a_pending_sensor_neither_alerts_nor_recovers():
    """After a restart we hold: no news is not the same as good news."""
    config = sensors_config()
    engine = AlertEngine(config)
    engine.evaluate(snapshot(NOW, sensors=[sensor_ok("3201b", COOL)]))
    engine.evaluate(snapshot(NOW + 700, sensors=[sensor_state("3201b", SensorState.STALE)]))

    pending = engine.evaluate(
        snapshot(NOW + 800, sensors=[sensor_state("3201b", SensorState.PENDING)])
    )
    assert pending.transitions == ()
    assert engine.states[sensor_key("3201b")].state == "alert"


# -- case 6: room crosses the threshold and stays high --------------------


def test_sustained_room_heat_alerts_once_after_the_debounce():
    config = sensors_config()
    engine = AlertEngine(config)

    assert engine.evaluate(room_snapshot(NOW, COOL)).transitions == ()

    # Crossing starts here but must persist for 600s before it counts.
    assert engine.evaluate(room_snapshot(NOW + 100, HOT)).transitions == ()
    assert engine.evaluate(room_snapshot(NOW + 400, HOT)).transitions == ()
    assert engine.evaluate(room_snapshot(NOW + 699, HOT)).transitions == ()

    fired = engine.evaluate(room_snapshot(NOW + 700, HOT))
    assert kinds(fired) == [TransitionKind.ALERT]
    assert fired.transitions[0].headline == ":warning: BioE 3201B temperature crossed 82°F."
    assert fired.transitions[0].key == room_key("b")


def test_a_room_that_stays_hot_is_not_mentioned_again():
    config = sensors_config()
    engine = AlertEngine(config)
    engine.evaluate(room_snapshot(NOW, HOT))
    engine.evaluate(room_snapshot(NOW + 700, HOT))

    for tick in range(1, 60):
        assert engine.evaluate(room_snapshot(NOW + 700 + tick * 60, HOT)).transitions == ()


def test_a_brief_spike_never_alerts():
    config = sensors_config()
    engine = AlertEngine(config)
    engine.evaluate(room_snapshot(NOW, COOL))
    engine.evaluate(room_snapshot(NOW + 60, HOT))
    engine.evaluate(room_snapshot(NOW + 120, HOT))
    assert engine.evaluate(room_snapshot(NOW + 180, COOL)).transitions == ()
    assert engine.evaluate(room_snapshot(NOW + 900, COOL)).transitions == ()


# -- case 7: jitter around the threshold must not flap --------------------


def test_jitter_around_the_threshold_never_alerts():
    """81.9 / 82.1 alternating: the crossing never lasts long enough."""
    config = sensors_config()
    engine = AlertEngine(config)
    transitions = []
    for tick in range(200):
        temperature = f_to_c(82.1 if tick % 2 else 81.9)
        transitions += list(engine.evaluate(room_snapshot(NOW + tick * 60, temperature)).transitions)
    assert transitions == []


def test_hysteresis_keeps_an_alerting_room_in_alert_below_the_trip_point():
    """Once alerting, 81.5F is still abnormal: recovery needs 80F or lower."""
    config = sensors_config()
    engine = AlertEngine(config)
    engine.evaluate(room_snapshot(NOW, HOT))
    engine.evaluate(room_snapshot(NOW + 700, HOT))

    for tick in range(1, 30):
        assessment = engine.evaluate(room_snapshot(NOW + 700 + tick * 60, WARM))
        assert assessment.transitions == ()
        assert assessment.room_temperature_abnormal("b") is True


# -- case 8: genuine recovery ---------------------------------------------


def test_a_room_that_truly_cools_recovers_once():
    config = sensors_config()
    engine = AlertEngine(config)
    engine.evaluate(room_snapshot(NOW, HOT))
    engine.evaluate(room_snapshot(NOW + 700, HOT))

    base = NOW + 800
    assert engine.evaluate(room_snapshot(base, COOL)).transitions == ()
    recovered = engine.evaluate(room_snapshot(base + 600, COOL))
    assert kinds(recovered) == [TransitionKind.RECOVERY]
    assert recovered.transitions[0].headline == (
        ":white_check_mark: BioE 3201B temperature returned to normal."
    )
    assert recovered.room_temperature_abnormal("b") is False

    for tick in range(1, 20):
        assert engine.evaluate(room_snapshot(base + 600 + tick * 60, COOL)).transitions == ()


def test_a_stale_sensor_does_not_fake_a_temperature_recovery():
    """Losing the sensor is not evidence that the room cooled down."""
    config = sensors_config()
    engine = AlertEngine(config)
    engine.evaluate(room_snapshot(NOW, HOT))
    engine.evaluate(room_snapshot(NOW + 700, HOT))

    lost = snapshot(NOW + 1400, sensors=[sensor_state("3201b", SensorState.STALE)])
    assessment = engine.evaluate(lost)
    assert [t.kind for t in assessment.transitions] == [TransitionKind.ALERT]
    assert assessment.transitions[0].key == sensor_key("3201b")
    assert engine.states[room_key("b")].state == "alert"


# -- cases 9-10: machine availability -------------------------------------


def test_a_machine_going_away_alerts_once(config, all_available):
    engine = AlertEngine(config)
    engine.evaluate(all_available(NOW))

    down = snapshot(
        NOW + 60,
        machines=[
            machine("deepthought", gpus=[gpu("0", 55.0)]),
            machine("gpu2", gpus=[gpu("0", 60.0)]),
            machine("gpu3", available=False),
        ],
    )
    assessment = engine.evaluate(down)
    assert kinds(assessment) == [TransitionKind.ALERT]
    assert "gpu3 is unavailable" in assessment.transitions[0].headline
    assert assessment.transitions[0].key == machine_key("gpu3")
    assert assessment.machine_unavailable("gpu3") is True
    assert assessment.machine_unavailable("gpu2") is False


def test_a_machine_that_stays_away_is_not_mentioned_again(config):
    engine = AlertEngine(config)

    def down(now):
        return snapshot(now, machines=[machine("gpu3", available=False)])

    assert kinds(engine.evaluate(down(NOW))) == [TransitionKind.ALERT]
    for tick in range(1, 100):
        assert engine.evaluate(down(NOW + tick * 30)).transitions == ()


def test_a_machine_coming_back_recovers_once(config):
    engine = AlertEngine(config)
    engine.evaluate(snapshot(NOW, machines=[machine("gpu3", available=False)]))

    back = engine.evaluate(
        snapshot(NOW + 60, machines=[machine("gpu3", gpus=[gpu("0", 55.0)])])
    )
    assert kinds(back) == [TransitionKind.RECOVERY]
    assert "gpu3 is reporting again" in back.transitions[0].headline


def test_an_unavailable_machines_gpus_do_not_alert(config):
    """No data is not the same as a cool GPU, nor a hot one."""
    engine = AlertEngine(config)
    engine.evaluate(snapshot(NOW, machines=[machine("gpu3", gpus=[gpu("0", 90.0)])]))
    engine.evaluate(snapshot(NOW + 200, machines=[machine("gpu3", gpus=[gpu("0", 90.0)])]))

    gone = engine.evaluate(snapshot(NOW + 300, machines=[machine("gpu3", available=False)]))
    assert [t.key for t in gone.transitions] == [machine_key("gpu3")]


def test_machine_availability_alerts_can_be_switched_off():
    config = make_config(availability={"alert_on_machine_unavailable": False})
    engine = AlertEngine(config)
    assessment = engine.evaluate(snapshot(NOW, machines=[machine("gpu3", available=False)]))
    assert assessment.transitions == ()
    assert assessment.machine_unavailable("gpu3") is True


# -- gpu temperature ------------------------------------------------------


def test_gpu_temperature_alerts_after_its_own_debounce(config):
    engine = AlertEngine(config)
    hot = [machine("gpu2", gpus=[gpu("0", 85.0)])]
    assert engine.evaluate(snapshot(NOW, machines=hot)).transitions == ()
    assert engine.evaluate(snapshot(NOW + 100, machines=hot)).transitions == ()

    fired = engine.evaluate(snapshot(NOW + 130, machines=hot))
    assert kinds(fired) == [TransitionKind.ALERT]
    assert fired.transitions[0].headline == ":warning: gpu2 GPU0 temperature crossed 80°C."


def test_each_gpu_is_tracked_independently(config):
    engine = AlertEngine(config)
    both = [machine("gpu2", gpus=[gpu("0", 85.0), gpu("1", 60.0)])]
    engine.evaluate(snapshot(NOW, machines=both))
    fired = engine.evaluate(snapshot(NOW + 130, machines=both))
    assert [t.key for t in fired.transitions] == ["gpu_temperature:gpu2:0"]
    assert fired.gpu_temperature_abnormal("gpu2", "0") is True
    assert fired.gpu_temperature_abnormal("gpu2", "1") is False


def test_a_gpu_without_a_temperature_reading_is_not_judged(config):
    engine = AlertEngine(config)
    nameless = [machine("gpu2", gpus=[gpu("0", None)])]
    for tick in range(10):
        assert engine.evaluate(snapshot(NOW + tick * 60, machines=nameless)).transitions == ()


# -- independent conditions -----------------------------------------------


def test_a_second_condition_crossing_produces_its_own_notification():
    config = sensors_config()
    engine = AlertEngine(config)

    hot_b = [sensor_ok("3201b", HOT), sensor_ok("3201a", COOL)]
    engine.evaluate(snapshot(NOW, sensors=hot_b))
    first = engine.evaluate(snapshot(NOW + 700, sensors=hot_b))
    assert [t.key for t in first.transitions] == [room_key("b")]

    both_hot = [sensor_ok("3201b", HOT), sensor_ok("3201a", HOT)]
    engine.evaluate(snapshot(NOW + 800, sensors=both_hot))
    second = engine.evaluate(snapshot(NOW + 1500, sensors=both_hot))
    assert [t.key for t in second.transitions] == [room_key("a")]
    # Both are abnormal now, so the dashboard flags both.
    assert second.room_temperature_abnormal("a") is True
    assert second.room_temperature_abnormal("b") is True


def test_the_hottest_sensor_in_a_room_decides_that_room():
    config = make_config(
        sensors=[
            {"id": "a1", "name": "north", "room": "a", "address": "AA:01"},
            {"id": "a2", "name": "south", "room": "a", "address": "AA:02"},
        ]
    )
    engine = AlertEngine(config)
    mixed = [sensor_ok("a1", COOL), sensor_ok("a2", HOT)]
    engine.evaluate(snapshot(NOW, sensors=mixed))
    fired = engine.evaluate(snapshot(NOW + 700, sensors=mixed))
    assert [t.key for t in fired.transitions] == [room_key("a")]


# -- case 16: persistence -------------------------------------------------


def test_state_survives_a_restart_without_re_alerting(tmp_path):
    config = sensors_config()
    path = str(tmp_path / "state.json")

    engine = AlertEngine(config)
    engine.evaluate(room_snapshot(NOW, HOT))
    fired = engine.evaluate(room_snapshot(NOW + 700, HOT))
    assert kinds(fired) == [TransitionKind.ALERT]
    save_state(path, engine.dump(), {"established": ["AA:02"]})

    restarted = AlertEngine(config, load_state(path).get("conditions"))
    for tick in range(20):
        assert restarted.evaluate(room_snapshot(NOW + 1000 + tick * 60, HOT)).transitions == ()


def test_a_condition_that_recovered_while_we_were_down_reports_once(tmp_path):
    config = sensors_config()
    path = str(tmp_path / "state.json")

    engine = AlertEngine(config)
    engine.evaluate(room_snapshot(NOW, HOT))
    engine.evaluate(room_snapshot(NOW + 700, HOT))
    save_state(path, engine.dump(), {})

    restarted = AlertEngine(config, load_state(path).get("conditions"))
    restarted.evaluate(room_snapshot(NOW + 2000, COOL))
    recovered = restarted.evaluate(room_snapshot(NOW + 2700, COOL))
    assert kinds(recovered) == [TransitionKind.RECOVERY]


def test_state_file_is_written_atomically_and_holds_no_secrets(tmp_path):
    path = tmp_path / "nested" / "state.json"
    save_state(str(path), {"room_temperature:b": {"state": "alert"}}, {"established": ["AA:02"]})

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["conditions"]["room_temperature:b"]["state"] == "alert"
    assert document["sensors"]["established"] == ["AA:02"]

    text = path.read_text(encoding="utf-8").lower()
    for secret in ("xoxb", "xapp", "token", "password", "ip"):
        assert secret not in text

    # No temporary files left behind.
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_a_corrupt_state_file_is_ignored_rather_than_fatal(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_state(str(path)) == {}


def test_a_missing_state_file_is_ignored(tmp_path):
    assert load_state(str(tmp_path / "absent.json")) == {}


def test_junk_inside_the_state_file_falls_back_to_normal(tmp_path):
    config = sensors_config()
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"conditions": {"room_temperature:b": "nonsense"}}), encoding="utf-8")
    engine = AlertEngine(config, load_state(str(path)).get("conditions"))
    assert engine.states[room_key("b")].state == "normal"


def test_states_for_departed_machines_are_pruned(config):
    engine = AlertEngine(config)
    engine.states["machine_unavailable:retired"] = engine._state("machine_unavailable:retired")
    engine.states["gpu_temperature:retired:0"] = engine._state("gpu_temperature:retired:0")
    engine.evaluate(snapshot(NOW, machines=[machine("gpu2", gpus=[gpu("0", 60.0)])]))
    assert "machine_unavailable:retired" not in engine.states
    assert "gpu_temperature:retired:0" not in engine.states


# -- threshold configuration edge cases -----------------------------------


def test_a_missing_threshold_section_disables_that_condition():
    config = make_config(
        sensors=[{"id": "3201b", "name": "B", "room": "b", "address": "AA:02"}],
        thresholds={"gpu_temperature": {"high": 80.0, "unit": "C"}},
    )
    engine = AlertEngine(config)
    for tick in range(20):
        assert engine.evaluate(room_snapshot(NOW + tick * 60, HOT)).transitions == ()


def test_a_disabled_threshold_is_not_evaluated():
    config = make_config(
        sensors=[{"id": "3201b", "name": "B", "room": "b", "address": "AA:02"}],
        thresholds={
            "room_temperature": {"high": 82.0, "unit": "F", "enabled": False},
        },
    )
    engine = AlertEngine(config)
    assessment = engine.evaluate(room_snapshot(NOW, HOT))
    assert assessment.transitions == ()
    assert assessment.room_temperature_abnormal("b") is False


@pytest.mark.parametrize("trigger", [0, 1, 30])
def test_zero_and_small_debounces_behave_sanely(trigger):
    config = make_config(
        sensors=[{"id": "3201b", "name": "B", "room": "b", "address": "AA:02"}],
        thresholds={
            "room_temperature": {
                "high": 82.0,
                "unit": "F",
                "trigger_after_seconds": trigger,
                "recovery_margin": 2.0,
            }
        },
    )
    engine = AlertEngine(config)
    # A zero delay commits on the first tick that sees the crossing; a longer
    # one commits on the tick that reaches the delay. Either way, exactly one.
    fired = kinds(engine.evaluate(room_snapshot(NOW, HOT)))
    fired += kinds(engine.evaluate(room_snapshot(NOW + trigger, HOT)))
    fired += kinds(engine.evaluate(room_snapshot(NOW + trigger + 60, HOT)))
    assert fired == [TransitionKind.ALERT]


# -- a data gap discards an in-flight debounce ----------------------------


def test_a_machine_outage_restarts_its_gpus_debounce(config):
    """We cannot call a violation "sustained" across a gap where we saw nothing."""
    engine = AlertEngine(config)
    hot = [machine("gpu2", gpus=[gpu("0", 88.0)])]

    engine.evaluate(snapshot(NOW, machines=hot))            # candidate starts
    engine.evaluate(snapshot(NOW + 60, machines=hot))       # 60s of 120s
    engine.evaluate(snapshot(NOW + 90, machines=[machine("gpu2", available=False)]))

    # Back, still hot: the 120s clock restarts rather than resuming at 90s.
    # (The machine's own availability recovery is a separate condition.)
    assert gpu_keys(engine.evaluate(snapshot(NOW + 100, machines=hot))) == []
    assert gpu_keys(engine.evaluate(snapshot(NOW + 150, machines=hot))) == []
    assert gpu_keys(engine.evaluate(snapshot(NOW + 220, machines=hot))) == [
        "gpu_temperature:gpu2:0"
    ]


def test_a_sensor_dropout_restarts_the_rooms_debounce():
    config = sensors_config()
    engine = AlertEngine(config)

    engine.evaluate(room_snapshot(NOW, HOT))
    engine.evaluate(room_snapshot(NOW + 300, HOT))          # 300s of 600s
    engine.evaluate(snapshot(NOW + 400, sensors=[sensor_state("3201b", SensorState.STALE)]))

    # The room never reaches 600s of continuous, observed violation.
    # (The sensor's own availability transitions are separate conditions.)
    assert room_keys(engine.evaluate(room_snapshot(NOW + 500, HOT))) == []
    assert room_keys(engine.evaluate(room_snapshot(NOW + 900, HOT))) == []
    assert room_keys(engine.evaluate(room_snapshot(NOW + 1100, HOT))) == [room_key("b")]


def test_a_committed_alert_survives_a_data_gap_unchanged():
    """Only the candidate is discarded; what we already told Slack stands."""
    config = sensors_config()
    engine = AlertEngine(config)
    engine.evaluate(room_snapshot(NOW, HOT))
    engine.evaluate(room_snapshot(NOW + 700, HOT))
    assert engine.states[room_key("b")].state == "alert"

    engine.evaluate(snapshot(NOW + 800, sensors=[sensor_state("3201b", SensorState.STALE)]))
    assert engine.states[room_key("b")].state == "alert"
    assert engine.states[room_key("b")].pending is None
