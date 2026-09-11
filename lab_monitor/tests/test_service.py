"""End-to-end wiring with every boundary mocked.

Exercises the composition -- poll, snapshot, evaluate, persist, notify -- and
what ``/labstatus`` returns.
"""

from __future__ import annotations

import csv
import json

from conftest import make_config
from test_netdata import NetdataClient, healthy_gpu_responses

import lab_monitor.__main__ as main
from lab_monitor.__main__ import Service
from lab_monitor.alerts import AlertEngine, load_state
from lab_monitor.govee import SensorStore
from lab_monitor.netdata import StatsdEmitter
from lab_monitor.slack import SlackNotifier

NOW = 1_700_000_000.0

HOSTS = ("DeepThought", "gpu2", "gpu3")


class Clock:
    """Hand-cranked, so nothing sleeps."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class FakeSlackClient:
    """Captures what would have been posted."""

    def __init__(self):
        self.messages = []

    def chat_postMessage(self, channel, text):
        self.messages.append({"channel": channel, "text": text})
        return {"ok": True}


class Agent:
    """A fake Netdata Parent answering relative to the test clock.

    Responses are per-request, so machines stay fresh as the clock advances
    until a test takes them offline.
    """

    def __init__(self, clock):
        self.clock = clock
        self.temperatures = dict.fromkeys(HOSTS, 60.0)
        self.offline_since = {}

    def set_temperature(self, host, celsius):
        self.temperatures[host] = celsius

    def take_offline(self, host, silent_for=900):
        """Freeze this host's newest data at ``silent_for`` seconds ago."""
        self.offline_since[host] = self.clock() - silent_for

    def bring_online(self, host):
        self.offline_since.pop(host, None)

    def __call__(self, url):
        import urllib.parse

        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        host = params.get("scope_nodes")
        context = params.get("scope_contexts")
        if host not in self.temperatures:
            return {"db": {}, "result": {}}

        last = self.offline_since.get(host, self.clock())
        responses = healthy_gpu_responses(
            hostname=host, last=last, temperature=self.temperatures[host]
        )
        payload = responses.get((host, context))
        if payload is None:
            return {"db": {}, "result": {}}
        return payload(params) if callable(payload) else payload


def build(tmp_path, clock=None, sensors=None, agent=None, config=None):
    """Assemble a Service with every boundary faked."""
    clock = clock or Clock()
    agent = agent or Agent(clock)
    config = config or make_config(
        sensors=sensors or [],
        state={"path": str(tmp_path / "state.json")},
        env={"NETDATA_DASHBOARD_URL": "https://netdata.example"},
    )
    slack = FakeSlackClient()
    state = load_state(config.state_path)
    service = Service(
        config,
        netdata_client=NetdataClient("http://fake", fetch=agent),
        sensor_store=SensorStore.from_state(state.get("sensors"), now=clock(), clock=clock),
        engine=AlertEngine(config, state.get("conditions")),
        statsd=StatsdEmitter(send=lambda line: None),
        notifier=SlackNotifier(slack, "C123"),
        clock=clock,
    )
    return service, agent, slack, clock, config


def dashboard_line(text, needle):
    """The dashboard row containing ``needle``, ignoring the headlines above."""
    body = text.split("```")[1]
    matches = [line for line in body.splitlines() if needle in line]
    assert matches, "no dashboard line containing %r in:\n%s" % (needle, text)
    return matches[0]


# -- /labstatus with three machines and no Govee sensors ------------------


def test_labstatus_shows_all_three_machines_grouped_by_room(tmp_path):
    service, _, _, _, _ = build(tmp_path)
    text = service.dashboard_message()

    assert "BioE 3201" in text
    for name in ("DeepThought", "gpu2", "gpu3"):
        assert name in text
    for room in ("BioE 3201A", "BioE 3201D", "Foyer"):
        assert room in text
    assert "No room sensors configured" in text
    assert "unavailable" not in text
    assert "(!)" not in text
    assert "<https://netdata.example|Full Netdata dashboard>" in text
    assert text.count("```") == 2


def test_labstatus_reuses_a_recent_poll_but_refreshes_a_stale_one(tmp_path):
    service, agent, _, clock, _ = build(tmp_path)
    service.poll()

    first = service.dashboard()
    clock.advance(2)
    assert service.dashboard() == first        # cached

    clock.advance(600)
    agent.set_temperature("gpu2", 77.0)
    assert "77°C" in service.dashboard()       # refreshed


def test_a_poll_with_nothing_wrong_posts_nothing(tmp_path):
    service, _, slack, clock, _ = build(tmp_path)
    for _ in range(10):
        service.poll()
        clock.advance(30)
    assert slack.messages == []


# -- cases 9/10: machine unavailable, one alert then silence --------------


def test_a_machine_going_offline_posts_one_alert_with_the_full_dashboard(tmp_path):
    service, agent, slack, clock, _ = build(tmp_path)
    service.poll()
    assert slack.messages == []

    clock.advance(60)
    agent.take_offline("gpu3")
    service.poll()

    assert len(slack.messages) == 1
    text = slack.messages[0]["text"]
    assert slack.messages[0]["channel"] == "C123"
    assert ":warning: gpu3 is unavailable" in text

    # Case 15: the message carries the whole dashboard, not just the fault.
    assert text.index(":warning:") < text.index("```")
    assert "ENVIRONMENT" in text
    assert "COMPUTE" in text
    assert "DeepThought" in text and "gpu2" in text
    assert "unavailable (!)" in text
    assert "<https://netdata.example|Full Netdata dashboard>" in text


def test_a_machine_that_stays_offline_is_not_mentioned_again(tmp_path):
    service, agent, slack, clock, _ = build(tmp_path)
    service.poll()
    agent.take_offline("gpu3")

    for _ in range(20):
        clock.advance(30)
        service.poll()

    assert len(slack.messages) == 1


def test_a_machine_coming_back_posts_one_recovery(tmp_path):
    service, agent, slack, clock, _ = build(tmp_path)
    service.poll()
    agent.take_offline("gpu3")
    clock.advance(30)
    service.poll()

    clock.advance(30)
    agent.bring_online("gpu3")
    service.poll()

    assert len(slack.messages) == 2
    assert ":white_check_mark: gpu3 is reporting again" in slack.messages[1]["text"]
    assert "(!)" not in slack.messages[1]["text"]


def test_two_faults_in_one_poll_share_one_message(tmp_path):
    """Both are announced; the dashboard below flags both."""
    service, agent, slack, clock, _ = build(tmp_path)
    service.poll()

    clock.advance(30)
    agent.take_offline("gpu2")
    agent.take_offline("gpu3")
    service.poll()

    assert len(slack.messages) == 1
    text = slack.messages[0]["text"]
    assert "gpu2 is unavailable" in text
    assert "gpu3 is unavailable" in text
    assert text.count("unavailable (!)") == 2


def test_alerts_and_recoveries_are_never_mixed_in_one_message(tmp_path):
    service, agent, slack, clock, _ = build(tmp_path)
    service.poll()
    agent.take_offline("gpu2")
    clock.advance(30)
    service.poll()
    assert len(slack.messages) == 1

    clock.advance(30)
    agent.bring_online("gpu2")                               # gpu2 recovers
    agent.take_offline("gpu3")                               # gpu3 fails
    service.poll()

    assert len(slack.messages) == 3
    assert ":warning: gpu3" in slack.messages[1]["text"]
    assert ":white_check_mark: gpu2" in slack.messages[2]["text"]


# -- gpu temperature end to end -------------------------------------------


def test_a_sustained_hot_gpu_alerts_once_with_the_flag_in_place(tmp_path):
    service, agent, slack, clock, _ = build(tmp_path)
    service.poll()

    agent.set_temperature("gpu2", 88.0)
    for _ in range(6):
        clock.advance(30)
        service.poll()

    assert len(slack.messages) == 1
    text = slack.messages[0]["text"]
    assert "gpu2 GPU0 temperature crossed 80°C" in text
    assert text.count("(!)") == 1
    assert "88°C (!)" in dashboard_line(text, "gpu2")


# -- case 16: restart with persisted state does not re-alert --------------


def test_a_restart_does_not_repeat_an_existing_alert(tmp_path):
    service, agent, slack, clock, config = build(tmp_path)
    service.poll()
    agent.take_offline("gpu3")
    clock.advance(30)
    service.poll()
    assert len(slack.messages) == 1

    # Restart: same config, same state file, same still-broken machine.
    clock.advance(60)
    restarted, _, slack2, clock2, _ = build(tmp_path, clock=clock, agent=agent, config=config)
    for _ in range(10):
        restarted.poll()
        clock.advance(30)

    assert slack2.messages == []
    assert restarted.dashboard().count("unavailable (!)") == 1


def test_a_restart_still_reports_a_change_that_happened_while_we_were_down(tmp_path):
    service, agent, slack, clock, config = build(tmp_path)
    service.poll()
    agent.take_offline("gpu3")
    clock.advance(30)
    service.poll()

    clock.advance(300)
    agent.bring_online("gpu3")
    restarted, _, slack2, _, _ = build(tmp_path, clock=clock, agent=agent, config=config)
    restarted.poll()

    assert len(slack2.messages) == 1
    assert ":white_check_mark: gpu3 is reporting again" in slack2.messages[0]["text"]


def test_the_state_file_is_json_and_carries_no_secrets(tmp_path):
    service, agent, _, clock, config = build(tmp_path)
    agent.take_offline("gpu3")
    service.poll()

    document = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert document["conditions"]["machine_unavailable:gpu3"]["state"] == "alert"
    assert document["sensors"] == {"established": []}
    text = json.dumps(document).lower()
    assert "xoxb" not in text and "xapp" not in text and "c123" not in text


# -- sensors end to end (cases 2-5) ---------------------------------------


SENSORS = [
    {"id": "3201a", "name": "A", "room": "a", "address": "AA:BB:CC:00:00:01"},
    {"id": "3201b", "name": "B", "room": "b", "address": "AA:BB:CC:00:00:02"},
]


def test_configured_but_absent_sensors_are_quiet_and_shown_as_not_yet_seen(tmp_path):
    service, _, slack, clock, _ = build(tmp_path, sensors=SENSORS)
    for _ in range(30):
        service.poll()
        clock.advance(60)

    assert slack.messages == []
    text = service.dashboard(max_age=0)
    assert text.count("not yet seen") == 2
    assert "(!)" not in text


def test_a_sensor_that_reports_then_dies_alerts_once_and_recovers_once(tmp_path):
    service, _, slack, clock, _ = build(tmp_path, sensors=SENSORS)

    service.sensors.record("AA:BB:CC:00:00:02", temperature_c=24.0, humidity_pct=40.0)
    service.poll()
    assert slack.messages == []
    assert "75.2°F" in service.dashboard(max_age=0)

    # Goes quiet past the 600s sensor timeout.
    clock.advance(700)
    service.poll()
    assert len(slack.messages) == 1
    assert "BioE 3201B sensor is unavailable" in slack.messages[0]["text"]
    assert "unavailable (!)" in slack.messages[0]["text"]

    for _ in range(20):
        clock.advance(60)
        service.poll()
    assert len(slack.messages) == 1

    # Comes back.
    service.sensors.record("AA:BB:CC:00:00:02", temperature_c=24.0)
    service.poll()
    assert len(slack.messages) == 2
    assert "BioE 3201B sensor is reporting again" in slack.messages[1]["text"]


def test_a_hot_room_alerts_once_after_its_debounce(tmp_path):
    service, _, slack, clock, _ = build(tmp_path, sensors=SENSORS)
    hot = 29.5   # 85.1F, over the 82F limit

    for _ in range(30):
        service.sensors.record("AA:BB:CC:00:00:02", temperature_c=hot, humidity_pct=39.0)
        service.poll()
        clock.advance(60)

    assert len(slack.messages) == 1
    text = slack.messages[0]["text"]
    assert text.startswith(":warning: BioE 3201B temperature crossed 82°F.")
    assert "ENVIRONMENT" in text and "COMPUTE" in text
    assert "(!)" in dashboard_line(text, "BioE 3201B")


def test_live_room_readings_are_exported_to_netdata(tmp_path):
    sent = []
    clock = Clock()
    config = make_config(sensors=SENSORS, state={"path": str(tmp_path / "state.json")})
    service = Service(
        config,
        netdata_client=NetdataClient("http://fake", fetch=Agent(Clock())),
        sensor_store=SensorStore(now=clock(), clock=clock),
        engine=AlertEngine(config),
        statsd=StatsdEmitter(send=sent.append),
        clock=clock,
    )

    service.sensors.record("AA:BB:CC:00:00:01", temperature_c=24.5, humidity_pct=41.0)
    service.poll()
    assert sent == [
        "labmonitor.room.a.3201a.temperature_c:24.5|g",
        "labmonitor.room.a.3201a.humidity_pct:41|g",
    ]

    # A stale sensor stops contributing rather than flat-lining the chart.
    sent.clear()
    clock.advance(700)
    service.poll()
    assert sent == []


# -- resilience -----------------------------------------------------------


def test_an_unreachable_netdata_yields_a_dashboard_of_unavailable_machines(tmp_path):
    def boom(url):
        raise OSError("connection refused")

    config = make_config(state={"path": str(tmp_path / "state.json")})
    service = Service(
        config,
        netdata_client=NetdataClient("http://fake", fetch=boom),
        sensor_store=SensorStore(now=NOW),
        engine=AlertEngine(config),
        statsd=StatsdEmitter(enabled=False),
        clock=Clock(),
    )
    text = service.dashboard()
    assert text.count("unavailable (!)") == 3
    assert "COMPUTE" in text


def test_a_slack_outage_does_not_break_the_poll_loop(tmp_path):
    class Broken:
        def chat_postMessage(self, channel, text):
            raise RuntimeError("slack is down")

    service, agent, _, clock, _ = build(tmp_path)
    service.notifier = SlackNotifier(Broken(), "C123")
    service.poll()
    agent.take_offline("gpu3")
    clock.advance(30)

    snapshot, assessment = service.poll()      # must not raise
    assert assessment.machine_unavailable("gpu3") is True


def test_a_notifier_with_no_channel_simply_does_nothing():
    assert SlackNotifier(FakeSlackClient(), None).enabled is False
    assert SlackNotifier(FakeSlackClient(), None).post("hello") is False


def test_an_unwritable_state_path_does_not_stop_monitoring(tmp_path, monkeypatch):
    config = make_config(state={"path": str(tmp_path / "state.json")})
    service = Service(
        config,
        netdata_client=NetdataClient("http://fake", fetch=Agent(Clock())),
        sensor_store=SensorStore(now=NOW),
        engine=AlertEngine(config),
        statsd=StatsdEmitter(enabled=False),
        clock=Clock(),
    )
    def refuse(path, conditions, sensors):
        raise OSError("read-only file system")

    monkeypatch.setattr(main, "save_state", refuse)
    snapshot, assessment = service.poll()
    assert len(snapshot.machines) == 3
    assert assessment.transitions == ()


# -- telemetry CSV --------------------------------------------------------


def telemetry_config(tmp_path, **kwargs):
    """The standard test lab, with the per-poll CSV enabled."""
    return make_config(
        state={"path": str(tmp_path / "state.json")},
        logging={"path": str(tmp_path / "lab_monitor.csv")},
        **kwargs,
    )


def telemetry_rows(tmp_path, name="lab_monitor.csv"):
    with open(tmp_path / name, newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def test_each_poll_appends_one_telemetry_row(tmp_path):
    config = telemetry_config(tmp_path)
    service, _, _, clock, _ = build(tmp_path, config=config)

    for _ in range(3):
        service.poll()
        clock.advance(30)

    table = telemetry_rows(tmp_path)
    assert len(table) == 4
    assert table[0][0] == "timestamp"
    assert "gpu.gpu2.0.temperature_c" in table[0]
    assert len({len(row) for row in table}) == 1


def test_an_offline_machine_logs_blanks_and_still_alerts(tmp_path):
    config = telemetry_config(tmp_path)
    service, agent, slack, clock, _ = build(tmp_path, config=config)
    service.poll()

    clock.advance(60)
    agent.take_offline("gpu3")
    service.poll()

    # Alerting is untouched by the log.
    assert len(slack.messages) == 1
    assert ":warning: gpu3 is unavailable" in slack.messages[0]["text"]

    table = telemetry_rows(tmp_path)
    column = table[0].index("gpu.gpu3.0.temperature_c")
    assert table[1][column] != ""        # while it was up
    assert table[2][column] == ""        # and no carried-forward value after


def test_no_logging_path_writes_no_telemetry(tmp_path):
    service, _, _, _, _ = build(tmp_path)
    service.poll()

    assert service.csvlog is None
    assert list(tmp_path.glob("*.csv")) == []


def test_status_inspects_without_recording_telemetry(tmp_path):
    config = telemetry_config(tmp_path)
    service, _, _, _, _ = build(tmp_path, config=config)
    read_only = Service(
        config,
        netdata_client=service.netdata,
        statsd=StatsdEmitter(enabled=False),
        persist=False,
    )

    assert read_only.csvlog is None
    read_only.dashboard(max_age=0)
    assert list(tmp_path.glob("*.csv")) == []


def test_an_unwritable_telemetry_log_does_not_stop_monitoring(tmp_path):
    config = telemetry_config(tmp_path)
    service, agent, slack, clock, _ = build(tmp_path, config=config)

    def refuse(snapshot):
        raise OSError("read-only file system")

    service.csvlog.append = refuse
    service.poll()
    clock.advance(60)
    agent.take_offline("gpu3")
    snapshot, assessment = service.poll()       # must not raise

    assert len(snapshot.machines) == 3
    assert assessment.machine_unavailable("gpu3") is True
    assert len(slack.messages) == 1


def test_room_readings_reach_both_the_log_and_statsd(tmp_path):
    sensors = [{"id": "3201a", "name": "A", "room": "a", "address": "AA:00:00:00:00:01"}]
    config = telemetry_config(tmp_path, sensors=sensors)
    service, _, _, clock, _ = build(tmp_path, config=config)

    sent = []
    service.statsd = StatsdEmitter(send=sent.append)
    service.sensors.record("AA:00:00:00:00:01", temperature_c=23.5, humidity_pct=41.0)
    service.poll()

    assert any("room.a.3201a.temperature_c" in line for line in sent)
    table = telemetry_rows(tmp_path)
    assert table[1][table[0].index("sensor.3201a.temperature_c")] == "23.5"
