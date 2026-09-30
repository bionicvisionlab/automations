"""Once-daily hot-weather advisory from the National Weather Service.

:class:`NwsClient` resolves the configured coordinates to their gridpoint
forecast and reads today's daytime high. :class:`WeatherAdvisor` asks it once a
day, on the first poll after :data:`CHECK_HOUR`, and posts one advisory to the
room channel when the high reaches ``[weather] notify_high``.

This is deliberately not an :class:`~lab_monitor.alerts.AlertEngine`
condition: a forecast is a single daily fact, so there is no debounce and no
recovery. The only state is the date of the last completed check, persisted so
a restart never repeats the day's message. A failed request completes nothing;
the check is retried after :data:`RETRY_SECONDS`.
"""

from __future__ import annotations

import datetime
import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

from .models import SensorState, convert_from_c, convert_to_c

NWS_BASE_URL = "https://api.weather.gov"

#: NWS rejects requests without a User-Agent and asks that it identify the app.
USER_AGENT = "bvl-lab-monitor (https://github.com/bionicvisionlab/automations)"

REQUEST_TIMEOUT_SECONDS = 10.0

#: Local hour (daemon clock) from which the day's check is due.
CHECK_HOUR = 7

#: How long to wait before retrying a failed check.
RETRY_SECONDS = 600

_SUN = ":sunny:"


class WeatherError(Exception):
    """Raised when the NWS cannot be reached or returns something unusable."""


@dataclass(frozen=True)
class DaytimeForecast:
    """Today's daytime forecast period, in the unit the NWS reported."""

    high: float
    unit: str
    summary: str | None = None

    def high_in(self, unit):
        """The high expressed in ``unit`` (``"C"`` or ``"F"``)."""
        if unit.upper() == self.unit:
            return self.high
        return round(convert_from_c(convert_to_c(self.high, self.unit), unit), 1)


class NwsClient:
    """Read-only client for ``api.weather.gov``.

    ``fetch`` takes a URL and returns decoded JSON; tests inject fixtures.
    """

    def __init__(
        self,
        latitude,
        longitude,
        timeout=REQUEST_TIMEOUT_SECONDS,
        fetch=None,
        base_url=NWS_BASE_URL,
    ):
        self.latitude = latitude
        self.longitude = longitude
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._fetch = fetch or self._http_get
        self._forecast_url = None

    # -- transport --------------------------------------------------------

    def _http_get(self, url):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise WeatherError("%s returned HTTP %s" % (url, exc.code)) from None
        except urllib.error.URLError as exc:
            raise WeatherError("could not reach %s: %s" % (url, exc.reason)) from None
        except (ValueError, socket.timeout) as exc:
            raise WeatherError("bad response from %s: %s" % (url, exc)) from None

    def _properties(self, url):
        payload = self._fetch(url)
        properties = payload.get("properties") if isinstance(payload, dict) else None
        if not isinstance(properties, dict):
            raise WeatherError("%s did not return a properties object" % url)
        return properties

    # -- queries ----------------------------------------------------------

    def forecast_url(self):
        """The gridpoint forecast URL for our coordinates, looked up once."""
        if self._forecast_url is None:
            # NWS redirects anything past four decimals; ask for its spelling.
            url = "%s/points/%s,%s" % (
                self.base_url,
                _coordinate(self.latitude),
                _coordinate(self.longitude),
            )
            forecast = self._properties(url).get("forecast")
            if not isinstance(forecast, str) or not forecast:
                raise WeatherError("%s gave no forecast URL" % url)
            self._forecast_url = forecast
        return self._forecast_url

    def daytime_forecast(self, day):
        """The daytime period starting on ``day``, or ``None`` if none is left.

        After the evening update the forecast starts with "Tonight", so late in
        the day there is honestly no daytime high for today.
        """
        try:
            return self._daytime_period(self.forecast_url(), day)
        except WeatherError:
            # The gridpoint rarely moves, but re-resolve it on the next attempt.
            self._forecast_url = None
            raise

    def _daytime_period(self, url, day):
        periods = self._properties(url).get("periods")
        if not isinstance(periods, list):
            raise WeatherError("%s gave no forecast periods" % url)

        for period in periods:
            if not isinstance(period, dict) or period.get("isDaytime") is not True:
                continue
            if _period_date(period) != day:
                continue
            high, unit = _period_temperature(period)
            summary = period.get("shortForecast")
            return DaytimeForecast(
                high=high,
                unit=unit,
                summary=summary if isinstance(summary, str) and summary else None,
            )
        return None


def _coordinate(value):
    """``-119.84487`` -> ``"-119.8449"``; NWS wants at most four decimals."""
    return ("%.4f" % value).rstrip("0").rstrip(".")


def _period_date(period):
    try:
        return datetime.datetime.fromisoformat(period.get("startTime")).date()
    except (TypeError, ValueError):
        raise WeatherError("bad forecast startTime %r" % period.get("startTime")) from None


def _period_temperature(period):
    """``(value, "F"|"C")`` from either NWS temperature encoding.

    The default is a bare number plus ``temperatureUnit``; the
    ``forecast_temperature_qv`` feature flag returns a quantitative value.
    """
    value = period.get("temperature")
    if isinstance(value, dict):
        code = str(value.get("unitCode", ""))
        unit = "C" if code.endswith("degC") else "F" if code.endswith("degF") else None
        value = value.get("value")
    else:
        unit = period.get("temperatureUnit")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeatherError("forecast period has no temperature")
    if unit not in ("C", "F"):
        raise WeatherError("forecast period has unknown temperature unit %r" % unit)
    return float(value), unit


class WeatherAdvisor:
    """Runs the day's forecast check and remembers that it has."""

    def __init__(self, config, client, last_checked=None, logger=None):
        self.config = config
        self.settings = config.weather
        self.client = client
        self.last_checked = last_checked
        self.logger = logger or logging.getLogger(__name__)
        self._retry_at = None

    # -- persistence ------------------------------------------------------

    def dump(self):
        """Persistable state: the ISO date of the last completed check."""
        return {"last_checked": self.last_checked}

    @classmethod
    def from_state(cls, config, client, raw, logger=None):
        """Rebuild from :meth:`dump` output, tolerating anything else."""
        last = raw.get("last_checked") if isinstance(raw, dict) else None
        return cls(
            config, client, last_checked=last if isinstance(last, str) else None, logger=logger
        )

    # -- the daily check --------------------------------------------------

    def due(self, now):
        """True on polls past :data:`CHECK_HOUR` until today's check completes."""
        local = datetime.datetime.fromtimestamp(now)
        if local.hour < CHECK_HOUR or self.last_checked == local.date().isoformat():
            return False
        return self._retry_at is None or now >= self._retry_at

    def run(self, now, snapshot, post):
        """Do today's check if it is due. Returns True when it completes.

        ``post`` sends one message to the room channel and returns whether
        Slack accepted it; a refusal is retried like a failed forecast.
        """
        if not self.due(now):
            return False
        today = datetime.datetime.fromtimestamp(now).date()
        unit = self.settings.unit

        try:
            forecast = self.client.daytime_forecast(today)
        except WeatherError as exc:
            return self._retry(now, "could not fetch the NWS forecast: %s", exc)

        if forecast is None:
            self.logger.info("NWS forecast has no daytime period left for %s", today)
        elif forecast.high_in(unit) >= self.settings.notify_high:
            if not post(self.message(forecast, snapshot)):
                return self._retry(now, "could not post the hot-weather advisory")
            self.logger.info(
                "posted hot-weather advisory: forecast high %s",
                _degrees(forecast.high_in(unit), unit),
            )
        else:
            self.logger.info(
                "forecast high %s is below %s; no hot-weather advisory",
                _degrees(forecast.high_in(unit), unit),
                _degrees(self.settings.notify_high, unit),
            )

        self.last_checked = today.isoformat()
        self._retry_at = None
        return True

    def _retry(self, now, message, *args):
        self._retry_at = now + RETRY_SECONDS
        self.logger.warning(message + "; retrying in %ds", *args, RETRY_SECONDS)
        return False

    # -- rendering --------------------------------------------------------

    def message(self, forecast, snapshot):
        """The advisory text; indoor temperature only if a sensor is live."""
        unit = self.settings.unit
        text = (
            "%s Hot weather advisory: the National Weather Service forecasts a high "
            "of %s today%s, at or above the %s advisory level."
            % (
                _SUN,
                _degrees(forecast.high_in(unit), unit),
                " (%s)" % forecast.summary if forecast.summary else "",
                _degrees(self.settings.notify_high, unit),
            )
        )
        hottest = self._hottest_indoor(snapshot)
        if hottest is not None:
            label, celsius = hottest
            display = self.config.display.temperature_unit
            text += " The warmest indoor reading right now is %.1f°%s in %s." % (
                convert_from_c(celsius, display),
                display,
                label,
            )
        return text

    def _hottest_indoor(self, snapshot):
        """``(label, celsius)`` of the hottest live sensor, or ``None``."""
        best = None
        for sensor in self.config.sensors:
            reading = snapshot.sensor(sensor.id) if snapshot is not None else None
            if reading is None or reading.state is not SensorState.OK:
                continue
            if reading.temperature_c is None:
                continue
            if best is None or reading.temperature_c > best[1]:
                best = (self._sensor_label(sensor), reading.temperature_c)
        return best

    def _sensor_label(self, sensor):
        room = self.config.room(sensor.room)
        if room is None:
            return sensor.name
        if len(self.config.sensors_in(sensor.room)) == 1:
            return room.name
        return "%s %s" % (room.name, sensor.name)


def _degrees(value, unit):
    """``95.0, "F"`` -> ``95°F``; keeps a meaningful decimal."""
    return "%s°%s" % (("%.1f" % value).rstrip("0").rstrip("."), unit)
