"""The once-daily NWS hot-weather advisory. No network, no real clock."""

from __future__ import annotations

import datetime
import io
import json
import logging
import urllib.error

import pytest
from conftest import make_config, sensor_ok, sensor_state, snapshot
from test_netdata import NetdataClient
from test_service import (
    COMPUTE,
    ROOM,
    SLACK_ENV,
    Agent,
    Clock,
    FakeSlackClient,
    run_with_fakes,
    sent_to,
)

import lab_monitor.__main__ as main
import lab_monitor.weather as weather
from lab_monitor.__main__ import Service
from lab_monitor.alerts import AlertEngine, load_state
from lab_monitor.govee import SensorStore
from lab_monitor.models import SensorState
from lab_monitor.netdata import StatsdEmitter
from lab_monitor.slack import SlackNotifier
from lab_monitor.weather import (
    RETRY_SECONDS,
    DaytimeForecast,
    NwsClient,
    WeatherAdvisor,
    WeatherError,
)

BEE = {"enabled": True, "latitude": 34.41305, "longitude": -119.84487, "notify_high": 82.0}

POINTS_URL = "https://api.weather.gov/points/34.413,-119.8449"
FORECAST_URL = "https://api.weather.gov/gridpoints/LOX/99,70/forecast"

SENSORS = [
    {"id": "3201a", "name": "A", "room": "a", "address": "AA:BB:CC:00:00:01"},
    {"id": "3201b", "name": "B", "room": "b", "address": "AA:BB:CC:00:00:02"},
]


def local(hour, minute=0, day=15):
    """A timestamp at this wall-clock time on the daemon's own clock."""
    return datetime.datetime(2026, 7, day, hour, minute).timestamp()


def period(day, daytime=True, high=95, unit="F", name=None):
    start = "2026-07-%02dT%s-07:00" % (day, "06:00:00" if daytime else "18:00:00")
    return {
        "name": name or ("Day %d" % day if daytime else "Night %d" % day),
        "startTime": start,
        "isDaytime": daytime,
        "temperature": high,
        "temperatureUnit": unit,
        "shortForecast": "Sunny",
    }


class FakeNws:
    """Answers the two NWS endpoints; flip ``fail`` to simulate an outage."""

    def __init__(self, periods=None):
        self.periods = periods if periods is not None else [
            period(15, high=95),
            period(15, daytime=False, high=70),
            period(16, high=80),
        ]
        self.fail = False
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if self.fail:
            raise WeatherError("could not reach api.weather.gov: timed out")
        if url == POINTS_URL:
            return {"properties": {"forecast": FORECAST_URL}}
        if url == FORECAST_URL:
            return {"properties": {"periods": self.periods}}
        raise AssertionError("unexpected URL %s" % url)

    @property
    def forecast_calls(self):
        return self.urls.count(FORECAST_URL)


def client(nws=None):
    return NwsClient(34.41305, -119.84487, fetch=nws or FakeNws())


# -- NWS client ------------------------------------------------------------


def test_a_check_resolves_the_gridpoint_then_reads_todays_high():
    nws = FakeNws()
    forecast = client(nws).daytime_forecast(datetime.date(2026, 7, 15))
    assert forecast == DaytimeForecast(high=95.0, unit="F")
    assert nws.urls == [POINTS_URL, FORECAST_URL]


def test_a_later_day_reads_its_own_daytime_period():
    assert client().daytime_forecast(datetime.date(2026, 7, 16)).high == 80.0


def test_the_night_period_is_never_mistaken_for_the_daytime_high():
    nws = FakeNws([period(15, daytime=False, high=70), period(15, high=88)])
    assert client(nws).daytime_forecast(datetime.date(2026, 7, 15)).high == 88.0


def test_no_daytime_period_left_for_today_is_none_not_an_error():
    """After the evening update the forecast opens with "Tonight"."""
    nws = FakeNws([period(15, daytime=False), period(16, high=101)])
    assert client(nws).daytime_forecast(datetime.date(2026, 7, 15)) is None


def test_a_quantitative_temperature_is_read_with_its_unit():
    hot = period(15)
    hot["temperature"] = {"unitCode": "wmoUnit:degC", "value": 35}
    del hot["temperatureUnit"]
    forecast = client(FakeNws([hot])).daytime_forecast(datetime.date(2026, 7, 15))
    assert (forecast.high, forecast.unit) == (35.0, "C")
    assert forecast.high_in("F") == 95.0


@pytest.mark.parametrize(
    "points, forecast, expected",
    [
        ({"properties": {}}, None, "gave no forecast URL"),
        ({"type": "Feature"}, None, "did not return a properties object"),
        ({"properties": {"forecast": FORECAST_URL}}, {"properties": {}}, "gave no forecast periods"),
        (
            {"properties": {"forecast": FORECAST_URL}},
            {"properties": {"periods": [dict(period(15), temperature=None)]}},
            "has no temperature",
        ),
        (
            {"properties": {"forecast": FORECAST_URL}},
            {"properties": {"periods": [dict(period(15), startTime="soon")]}},
            "bad forecast startTime",
        ),
    ],
)
def test_an_unusable_response_is_a_weather_error(points, forecast, expected):
    responses = {POINTS_URL: points, FORECAST_URL: forecast}
    nws_client = NwsClient(34.41305, -119.84487, fetch=responses.__getitem__)
    with pytest.raises(WeatherError) as excinfo:
        nws_client.daytime_forecast(datetime.date(2026, 7, 15))
    assert expected in str(excinfo.value)


def test_http_requests_identify_themselves_to_the_nws(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("User-agent"), timeout))
        return io.BytesIO(json.dumps({"properties": {"forecast": FORECAST_URL}}).encode())

    monkeypatch.setattr(weather.urllib.request, "urlopen", urlopen)
    assert NwsClient(34.41305, -119.84487, timeout=3).forecast_url() == FORECAST_URL
    assert seen == [(POINTS_URL, weather.USER_AGENT, 3)]


def test_transport_failures_become_weather_errors(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "busy", None, None)

    monkeypatch.setattr(weather.urllib.request, "urlopen", urlopen)
    with pytest.raises(WeatherError) as excinfo:
        NwsClient(34.41305, -119.84487).forecast_url()
    assert "HTTP 503" in str(excinfo.value)


# -- the daily check ---------------------------------------------------------


class Posts(list):
    """Stands in for ``SlackNotifier.post_advisory``."""

    accept = True

    def __call__(self, text):
        self.append(text)
        return self.accept


def advisor(nws=None, weather_settings=None, sensors=(), last_checked=None, **kwargs):
    config = make_config(weather=weather_settings or BEE, sensors=list(sensors), **kwargs)
    return WeatherAdvisor(config, client(nws), last_checked=last_checked)


def test_nothing_is_fetched_before_seven():
    nws, posts = FakeNws(), Posts()
    advice = advisor(nws)
    assert advice.run(local(6, 59), snapshot(local(6, 59)), posts) is False
    assert nws.urls == [] and posts == []


def test_the_first_poll_after_seven_posts_one_advisory_for_the_day():
    nws, posts = FakeNws(), Posts()
    advice = advisor(nws)

    assert advice.run(local(7, 1), snapshot(local(7, 1)), posts) is True
    for hour in (8, 12, 15, 23):
        assert advice.run(local(hour), snapshot(local(hour)), posts) is False

    assert nws.forecast_calls == 1
    assert len(posts) == 1
    assert posts[0] == (
        "Hot day expected: NWS forecasts a high of 95°F at UCSB today. "
        "Consider working from home today if you can."
    )
    assert advice.last_checked == "2026-07-15"


def test_a_high_exactly_at_the_limit_posts():
    posts = Posts()
    advisor(FakeNws([period(15, high=82)])).run(local(7), snapshot(local(7)), posts)
    assert len(posts) == 1


def test_a_mild_day_completes_the_check_without_posting():
    nws, posts = FakeNws([period(15, high=81)]), Posts()
    advice = advisor(nws)
    assert advice.run(local(7), snapshot(local(7)), posts) is True
    advice.run(local(9), snapshot(local(9)), posts)
    assert posts == []
    assert nws.forecast_calls == 1


def test_the_next_day_is_checked_again():
    nws, posts = FakeNws([period(15, high=95), period(16, high=97)]), Posts()
    advice = advisor(nws)
    advice.run(local(7), snapshot(local(7)), posts)
    advice.run(local(7, 30, day=16), snapshot(local(7, 30, day=16)), posts)
    assert len(posts) == 2
    assert "97°F" in posts[1]


def test_the_limit_is_compared_in_the_configured_unit():
    """35°C is 95°F: over a 34°C limit, and rendered in Celsius."""
    posts = Posts()
    advice = advisor(weather_settings=dict(BEE, notify_high=34, unit="C"))
    advice.run(local(7), snapshot(local(7)), posts)
    assert "high of 35°C at UCSB today" in posts[0]


def test_the_advisory_includes_the_hottest_live_indoor_reading():
    posts = Posts()
    advice = advisor(sensors=SENSORS + [{"id": "3201c", "name": "C", "room": "c"}])
    now = local(7)
    advice.run(
        now,
        snapshot(
            now,
            sensors=[
                sensor_ok("3201a", 24.0),
                sensor_ok("3201b", 26.0),
                sensor_state("3201c", SensorState.NEVER_SEEN),
            ],
        ),
        posts,
    )
    assert posts[0] == (
        "Hot day expected: NWS forecasts a high of 95°F at UCSB today. "
        "Warmest indoor reading right now: 78.8°F in BioE 3201B. "
        "Consider working from home today if you can."
    )


def test_the_indoor_reading_uses_the_display_unit():
    posts = Posts()
    advice = advisor(sensors=SENSORS, display={"temperature_unit": "C"})
    advice.run(local(7), snapshot(local(7), sensors=[sensor_ok("3201a", 24.0)]), posts)
    assert "24.0°C in BioE 3201A" in posts[0]


def test_without_a_live_sensor_the_indoor_temperature_is_left_out():
    posts = Posts()
    advice = advisor(sensors=SENSORS)
    advice.run(
        local(7),
        snapshot(
            local(7),
            sensors=[
                sensor_state("3201a", SensorState.STALE, last_seen=1.0),
                sensor_state("3201b", SensorState.PENDING),
            ],
        ),
        posts,
    )
    assert "indoor" not in posts[0]
    assert posts[0].endswith("at UCSB today. Consider working from home today if you can.")


def test_a_failed_request_logs_and_retries_later_without_completing(caplog):
    nws, posts = FakeNws(), Posts()
    nws.fail = True
    advice = advisor(nws)

    with caplog.at_level(logging.WARNING):
        assert advice.run(local(7), snapshot(local(7)), posts) is False
    assert "could not fetch the NWS forecast" in caplog.text
    assert advice.last_checked is None

    nws.fail = False
    advice.run(local(7) + RETRY_SECONDS - 1, snapshot(local(7)), posts)
    assert posts == []                          # not yet: waiting out the retry

    assert advice.run(local(7) + RETRY_SECONDS, snapshot(local(7)), posts) is True
    assert len(posts) == 1


def test_a_refused_slack_post_is_retried_like_a_failed_request():
    posts = Posts()
    posts.accept = False
    advice = advisor()
    assert advice.run(local(7), snapshot(local(7)), posts) is False
    assert advice.last_checked is None

    posts.accept = True
    assert advice.run(local(7) + RETRY_SECONDS, snapshot(local(7)), posts) is True
    assert len(posts) == 2


def test_no_daytime_forecast_left_completes_the_day_quietly():
    posts = Posts()
    advice = advisor(FakeNws([period(15, daytime=False), period(16, high=110)]))
    assert advice.run(local(20), snapshot(local(20)), posts) is True
    assert posts == []


def test_a_date_already_checked_is_not_checked_again():
    nws, posts = FakeNws(), Posts()
    advisor(nws, last_checked="2026-07-15").run(local(7), snapshot(local(7)), posts)
    assert nws.urls == [] and posts == []


@pytest.mark.parametrize(
    "day, reason",
    [(18, "Saturday"), (19, "Sunday"), (3, "Independence Day, observed on Friday")],
)
def test_weekends_and_holidays_complete_the_day_without_asking(day, reason, caplog):
    nws, posts = FakeNws([period(day, high=110)]), Posts()
    advice = advisor(nws)
    with caplog.at_level(logging.INFO):
        assert advice.run(local(7, day=day), snapshot(local(7, day=day)), posts) is True
    assert nws.urls == [] and posts == [], reason
    assert advice.last_checked == "2026-07-%02d" % day
    assert "not a workday" in caplog.text


def test_federal_holidays_follow_the_observance_rules():
    assert weather.federal_holidays(2026) == {
        datetime.date(2026, 1, 1),
        datetime.date(2026, 1, 19),    # third Monday in January
        datetime.date(2026, 2, 16),    # third Monday in February
        datetime.date(2026, 5, 25),    # last Monday in May
        datetime.date(2026, 6, 19),
        datetime.date(2026, 7, 3),     # July 4 is a Saturday
        datetime.date(2026, 9, 7),     # first Monday in September
        datetime.date(2026, 10, 12),   # second Monday in October
        datetime.date(2026, 11, 11),
        datetime.date(2026, 11, 26),   # fourth Thursday in November
        datetime.date(2026, 12, 25),
    }
    assert datetime.date(2027, 7, 5) in weather.federal_holidays(2027)   # July 4 is a Sunday


@pytest.mark.parametrize(
    "day, expected",
    [
        (datetime.date(2026, 7, 15), True),
        (datetime.date(2026, 7, 3), False),
        (datetime.date(2027, 12, 31), False),   # New Year's Day 2028 is a Saturday
        (datetime.date(2028, 1, 3), True),      # ...so the Monday after is not off
    ],
)
def test_is_workday(day, expected):
    assert weather.is_workday(day) is expected


@pytest.mark.parametrize("raw", [None, "2026-07-15", {"last_checked": 20260715}, {}])
def test_junk_weather_state_starts_fresh(raw):
    restored = WeatherAdvisor.from_state(make_config(weather=BEE), client(), raw)
    assert restored.last_checked is None


# -- inside the daemon -------------------------------------------------------


def build_service(tmp_path, clock, nws, sensors=(), config=None):
    """A Service whose poll includes the advisory, restored from the state file."""
    config = config or make_config(
        sensors=list(sensors),
        weather=BEE,
        state={"path": str(tmp_path / "state.json")},
    )
    slack = FakeSlackClient()
    state = load_state(config.state_path)
    store = SensorStore.from_state(state.get("sensors"), now=clock(), clock=clock)
    service = Service(
        config,
        netdata_client=NetdataClient("http://fake", fetch=Agent(clock)),
        sensor_store=store,
        engine=AlertEngine(config, state.get("conditions")),
        statsd=StatsdEmitter(send=lambda line: None),
        notifier=SlackNotifier(slack, ROOM, COMPUTE),
        weather=WeatherAdvisor.from_state(config, client(nws), state.get("weather")),
        clock=clock,
    )
    return service, slack, store, config


def test_the_advisory_goes_to_the_room_channel_on_its_own(tmp_path):
    clock, nws = Clock(local(7, 0, day=15)), FakeNws()
    service, slack, store, _ = build_service(tmp_path, clock, nws, sensors=SENSORS)
    store.record("AA:BB:CC:00:00:01", temperature_c=25.0, now=clock())

    for _ in range(20):
        service.poll()
        clock.advance(30)

    assert sent_to(slack, COMPUTE) == []
    [text] = sent_to(slack, ROOM)
    assert text.startswith("Hot day expected: NWS forecasts")
    assert "77.0°F in BioE 3201A" in text
    assert "```" not in text                    # an advisory, not a dashboard


def test_the_checked_date_survives_a_restart_so_the_day_is_not_repeated(tmp_path):
    clock, nws = Clock(local(7, 5)), FakeNws()
    service, slack, _, config = build_service(tmp_path, clock, nws)
    service.poll()
    assert len(slack.messages) == 1

    document = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert document["weather"] == {"last_checked": "2026-07-15"}
    assert "weather" not in document["conditions"]

    clock.advance(3600)
    restarted, slack2, _, _ = build_service(tmp_path, clock, nws, config=config)
    restarted.poll()
    assert slack2.messages == []
    assert nws.forecast_calls == 1


def test_a_failed_check_is_not_recorded_so_a_restart_tries_again(tmp_path):
    clock, nws = Clock(local(7, 5)), FakeNws()
    nws.fail = True
    service, slack, _, config = build_service(tmp_path, clock, nws)
    snap, _ = service.poll()                    # the poll itself still succeeds
    assert len(snap.machines) == 3
    assert slack.messages == []
    assert load_state(config.state_path)["weather"] == {"last_checked": None}

    nws.fail = False
    restarted, slack2, _, _ = build_service(tmp_path, clock, nws, config=config)
    restarted.poll()
    assert len(slack2.messages) == 1


# -- command wiring ----------------------------------------------------------


def run_and_capture_weather(monkeypatch, tmp_path, env, weather_settings=BEE, state=None):
    services = []
    run_with_fakes(monkeypatch, lambda app, app_token, stop_event: stop_event.set())
    monkeypatch.setattr(main.Service, "run_forever", lambda self: services.append(self))
    path = tmp_path / "state.json"
    if state is not None:
        path.write_text(json.dumps(state), encoding="utf-8")
    config = make_config(weather=weather_settings, state={"path": str(path)}, env=env)
    assert main.command_run(config) == 0
    return services[0].weather


def test_command_run_restores_the_advisor_from_the_state_file(monkeypatch, tmp_path):
    advice = run_and_capture_weather(
        monkeypatch,
        tmp_path,
        dict(SLACK_ENV, LAB_MONITOR_SLACK_ROOM_CHANNEL_ID=ROOM),
        state={"weather": {"last_checked": "2026-07-15"}},
    )
    assert isinstance(advice, WeatherAdvisor)
    assert advice.last_checked == "2026-07-15"
    assert (advice.client.latitude, advice.client.longitude) == (34.41305, -119.84487)


def test_command_run_skips_the_advisory_without_a_room_channel(monkeypatch, tmp_path, caplog):
    env = {
        "LAB_MONITOR_SLACK_BOT_TOKEN": "xoxb-1",
        "LAB_MONITOR_SLACK_APP_TOKEN": "xapp-1",
        "LAB_MONITOR_SLACK_COMPUTE_CHANNEL_ID": COMPUTE,
    }
    with caplog.at_level(logging.WARNING, logger="lab_monitor"):
        assert run_and_capture_weather(monkeypatch, tmp_path, env) is None
    assert "hot-weather advisory disabled" in caplog.text


def test_command_run_leaves_a_disabled_advisory_off(monkeypatch, tmp_path):
    assert run_and_capture_weather(
        monkeypatch, tmp_path, SLACK_ENV, weather_settings={"enabled": False}
    ) is None


def test_status_never_consults_the_weather(monkeypatch, tmp_path, capsys):
    def refuse(*args, **kwargs):
        raise AssertionError("status must not call the NWS")

    monkeypatch.setattr(main, "NwsClient", refuse)
    monkeypatch.setattr(main.NetdataClient, "_http_get", lambda self, url: {})
    config = make_config(weather=BEE, state={"path": str(tmp_path / "state.json")})
    assert main.command_status(config) == 0


def test_check_config_reports_the_advisory(capsys):
    main.command_check_config(make_config(weather=BEE, env=SLACK_ENV))
    out = capsys.readouterr().out
    assert "weather advisory" in out
    assert "location        34.41305, -119.84487" in out
    assert "notify high     82.0°F" in out
    assert "room channel C1" in out


def test_check_config_reports_a_disabled_advisory(capsys):
    main.command_check_config(make_config())
    out = capsys.readouterr().out
    assert "weather advisory\n  <disabled>" in out
