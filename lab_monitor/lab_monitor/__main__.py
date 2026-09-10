"""LabMonitor entry point: the service loop and the command line.

    python -m lab_monitor run             # the systemd service
    python -m lab_monitor status          # print the dashboard once
    python -m lab_monitor check-config    # validate config, resolve env vars
    python -m lab_monitor discover-govee  # find sensor addresses during setup
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from .alerts import AlertEngine, load_state, save_state
from .config import ConfigError, load_config
from .govee import DISCOVERY_SECONDS, GoveeReceiver, SensorStore, discover
from .models import SensorState
from .netdata import NetdataClient, StatsdEmitter
from .slack import (
    COMMAND_MAX_AGE_SECONDS,
    SlackNotifier,
    build_app,
    build_web_client,
    run_socket_mode,
)
from .status import build_snapshot, render_dashboard, render_message

LOG = logging.getLogger("lab_monitor")


class Service:
    """Ties the adapters together and owns the current view of the lab.

    One lock guards the whole poll, so ``/labstatus`` never sees a half-update.
    """

    def __init__(
        self,
        config,
        netdata_client=None,
        sensor_store=None,
        engine=None,
        statsd=None,
        notifier=None,
        clock=time.time,
        persist=True,
    ):
        self.config = config
        self.clock = clock
        self.persist = persist
        self.netdata = netdata_client or NetdataClient(
            config.netdata.url, timeout=config.netdata.timeout_seconds
        )
        self.sensors = sensor_store or SensorStore(clock=clock)
        self.engine = engine or AlertEngine(config)
        self.statsd = statsd or StatsdEmitter(
            host=config.netdata.statsd_host,
            port=config.netdata.statsd_port,
            prefix=config.netdata.statsd_prefix,
            enabled=config.netdata.statsd_enabled,
        )
        self.notifier = notifier
        self.snapshot = None
        self.assessment = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- one cycle --------------------------------------------------------

    def poll(self, now=None):
        """Collect, evaluate, persist and notify. Returns the new state."""
        with self._lock:
            return self._poll_locked(self.clock() if now is None else now)

    def _poll_locked(self, now):
        snapshot = build_snapshot(self.config, self.netdata, self.sensors, now)
        self._export(snapshot)
        assessment = self.engine.evaluate(snapshot)
        self.snapshot, self.assessment = snapshot, assessment

        self._save()
        if assessment.transitions and self.notifier is not None:
            dashboard = render_dashboard(self.config, snapshot, assessment)
            self.notifier.post_transitions(
                assessment.transitions, dashboard, self.config.netdata.dashboard_url
            )
        return snapshot, assessment

    def _export(self, snapshot):
        """Feed live room readings to Netdata via StatsD.

        Stale sensors contribute nothing, so charts gap rather than flat-line.
        """
        for sensor in self.config.sensors:
            reading = snapshot.sensor(sensor.id)
            if reading is None or reading.state is not SensorState.OK:
                continue
            self.statsd.room_reading(
                "%s.%s" % (sensor.room, sensor.id),
                temperature_c=reading.temperature_c,
                humidity_pct=reading.humidity_pct,
                battery_pct=reading.battery_pct,
            )

    def _save(self):
        if not self.persist:
            return
        try:
            save_state(self.config.state_path, self.engine.dump(), self.sensors.dump())
        except OSError as exc:
            LOG.warning("could not write state file %s: %s", self.config.state_path, exc)

    # -- reads ------------------------------------------------------------

    def current(self, max_age=COMMAND_MAX_AGE_SECONDS):
        """Return a snapshot no older than ``max_age``, polling if needed."""
        with self._lock:
            now = self.clock()
            if self.snapshot is None or (now - self.snapshot.taken_at) > max_age:
                return self._poll_locked(now)
            return self.snapshot, self.assessment

    def dashboard(self, max_age=COMMAND_MAX_AGE_SECONDS):
        """The current dashboard as plain text."""
        snapshot, assessment = self.current(max_age)
        return render_dashboard(self.config, snapshot, assessment)

    def dashboard_message(self, max_age=COMMAND_MAX_AGE_SECONDS):
        """The current dashboard, formatted for Slack."""
        return render_message(
            self.dashboard(max_age), dashboard_url=self.config.netdata.dashboard_url
        )

    # -- loop -------------------------------------------------------------

    def run_forever(self):
        """Poll on the configured interval until :meth:`stop` is called."""
        interval = max(self.config.poll_interval_seconds, 1)
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:
                LOG.exception("poll failed; continuing")
            self._stop.wait(interval)

    def stop(self):
        """Ask the poll loop to finish after the current wait."""
        self._stop.set()


# -- commands --------------------------------------------------------------


def command_run(config):
    """Run the daemon: BLE scan, poll loop, and the Slack app."""
    state = load_state(config.state_path)
    engine = AlertEngine(config, state.get("conditions"))
    sensors = SensorStore.from_state(state.get("sensors"))

    notifier = None
    if config.slack.bot_token and config.slack.channel_id:
        notifier = SlackNotifier(build_web_client(config.slack.bot_token), config.slack.channel_id, LOG)
    else:
        LOG.warning(
            "Slack notifications disabled: set LAB_MONITOR_SLACK_BOT_TOKEN and "
            "LAB_MONITOR_SLACK_CHANNEL_ID to enable them"
        )

    service = Service(config, sensor_store=sensors, engine=engine, notifier=notifier)

    receiver = None
    addresses = [s.address for s in config.sensors if s.has_address]
    if addresses:
        receiver = GoveeReceiver(sensors, addresses=addresses, logger=LOG)
        receiver.start()
        LOG.info("BLE active scan started for %d sensor(s)", len(addresses))
    else:
        LOG.info("no Govee sensors configured; skipping BLE scan")

    def shutdown(_signum=None, _frame=None):
        LOG.info("shutting down")
        service.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    if config.slack.configured:
        # Bolt's Socket Mode handler blocks, so the poll loop gets a thread.
        poller = threading.Thread(target=service.run_forever, name="poll", daemon=True)
        poller.start()
        LOG.info("connecting to Slack in Socket Mode")
        try:
            run_socket_mode(build_app(service, config.slack, LOG), config.slack.app_token)
        finally:
            service.stop()
    else:
        LOG.warning(
            "Slack app disabled: set LAB_MONITOR_SLACK_BOT_TOKEN and "
            "LAB_MONITOR_SLACK_APP_TOKEN to serve /labstatus"
        )
        service.run_forever()

    if receiver is not None:
        receiver.stop()
    return 0


def command_status(config):
    """Print the dashboard once, without touching the state file."""
    state = load_state(config.state_path)
    service = Service(
        config,
        sensor_store=SensorStore.from_state(state.get("sensors")),
        engine=AlertEngine(config, state.get("conditions")),
        persist=False,
    )
    print(service.dashboard(max_age=0))
    return 0


def command_check_config(config):
    """Report what the configuration resolved to, and what is missing."""
    print("config file       %s" % (config.source_path or "<inline>"))
    print("site              %s" % config.site_name)
    print("netdata           %s" % config.netdata.url)
    print("netdata dashboard %s" % (config.netdata.dashboard_url or "<not set>"))
    print("statsd            %s:%d prefix=%s enabled=%s" % (
        config.netdata.statsd_host,
        config.netdata.statsd_port,
        config.netdata.statsd_prefix,
        config.netdata.statsd_enabled,
    ))
    print("state file        %s" % config.state_path)
    print("poll interval     %ds" % config.poll_interval_seconds)
    print("")

    print("rooms")
    for room in config.ordered_rooms():
        print("  %-12s %s" % (room.id, room.name))
    print("")

    print("machines")
    for machine in config.machines:
        address = config.machine_addresses.get(machine.id)
        if machine.address_env:
            address = "%s=%s" % (machine.address_env, address or "<unset>")
        else:
            address = "<no address_env>"
        print("  %-12s room=%-8s netdata=%-14s %s%s" % (
            machine.id,
            machine.room,
            machine.netdata_hostname,
            address,
            "  [parent]" if machine.parent else "",
        ))
    print("")

    print("sensors")
    if not config.sensors:
        print("  <none configured>")
    for sensor in config.sensors:
        print("  %-12s room=%-8s address=%s" % (
            sensor.id, sensor.room, sensor.address or "<not yet known>"
        ))
    print("")

    print("thresholds")
    for name, threshold in sorted(config.thresholds.items()):
        print("  %-18s high=%s°%s trigger=%ds recover_margin=%s enabled=%s" % (
            name,
            threshold.high,
            threshold.unit,
            threshold.trigger_after_seconds,
            threshold.recovery_margin,
            threshold.enabled,
        ))
    print("")

    print("slack")
    print("  bot token       %s" % ("set" if config.slack.bot_token else "MISSING"))
    print("  app token       %s" % ("set" if config.slack.app_token else "MISSING"))
    print("  channel id      %s" % (config.slack.channel_id or "MISSING"))

    if config.warnings:
        print("")
        print("warnings")
        for warning in config.warnings:
            print("  ! %s" % warning)
    return 0


def command_discover(seconds):
    """Scan for Govee sensors and print their addresses."""
    print("Active-scanning for Govee sensors for %.0f seconds..." % seconds)
    print("")
    try:
        discover(seconds)
    except ImportError as exc:
        print("Bluetooth support is not installed: %s" % exc, file=sys.stderr)
        print("Install it with: pip install '.[ble]'", file=sys.stderr)
        return 1
    except Exception as exc:
        print("Scan failed: %s" % exc, file=sys.stderr)
        return 1
    return 0


# -- cli -------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m lab_monitor",
        description="BVL lab environment and GPU monitor.",
    )
    parser.add_argument("--config", help="path to lab_monitor.toml (default: $LAB_MONITOR_CONFIG)")
    parser.add_argument("--log-level", default="INFO", help="logging level (default: INFO)")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the monitoring service (default)")
    sub.add_parser("status", help="print the current dashboard and exit")
    sub.add_parser("check-config", help="validate configuration and exit")
    discover_parser = sub.add_parser("discover-govee", help="scan for nearby Govee sensors")
    discover_parser.add_argument(
        "--seconds", type=float, default=DISCOVERY_SECONDS, help="scan duration"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    command = args.command or "run"
    if command == "discover-govee":
        return command_discover(args.seconds)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("LabMonitor configuration error: %s" % exc, file=sys.stderr)
        return 2

    for warning in config.warnings:
        LOG.warning("%s", warning)

    if command == "check-config":
        return command_check_config(config)
    if command == "status":
        return command_status(config)
    return command_run(config)


if __name__ == "__main__":
    sys.exit(main())
