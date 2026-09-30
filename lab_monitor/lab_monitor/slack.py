"""Slack surface: the status commands and transition notifications.

``/labstatus`` shows the whole lab, ``/roomstatus`` the environment and
``/gpustatus`` the compute half. Unsolicited transitions go to the audience
they concern: room heat and sensor availability to the room channel, GPU
temperature and machine availability to the compute channel, each with only
its own half of the dashboard. The daily hot-weather advisory also goes to the
room channel, as a bare message with no dashboard.

LabMonitor needs its own Slack app: Socket Mode binds one app-level token to
one process, so it cannot share DeadlineWatcher's. ``slack_bolt`` is imported
lazily so the package works without it installed.
"""

from __future__ import annotations

from .alerts import SUITE_TEMPERATURE_KEY
from .models import TransitionKind
from .status import render_message

#: How stale a cached snapshot may be before a status command forces a refresh.
COMMAND_MAX_AGE_SECONDS = 20.0


class SlackNotifier:
    """Posts transition messages to the room and compute channels.

    Either channel may be missing; its messages are then logged and dropped.
    Failures are logged and swallowed; a Slack outage must not stop monitoring.
    """

    def __init__(self, client, room_channel_id=None, compute_channel_id=None, logger=None):
        self.client = client
        self.room_channel_id = room_channel_id
        self.compute_channel_id = compute_channel_id
        self.logger = logger

    @property
    def enabled(self):
        """True when we have somewhere to post."""
        return bool(self.client and (self.room_channel_id or self.compute_channel_id))

    def post(self, channel_id, text):
        """Post one message. Returns True if Slack accepted it."""
        if not (self.client and channel_id):
            self._log("no Slack channel configured; dropping message")
            return False
        try:
            self.client.chat_postMessage(channel=channel_id, text=text)
            return True
        except Exception as exc:
            self._log("could not post to Slack: %s", exc)
            return False

    def post_advisory(self, text):
        """Post a standalone advisory (no dashboard) to the room channel."""
        return self.post(self.room_channel_id, text)

    def post_transitions(self, transitions, room_dashboard, gpu_dashboard, dashboard_url=None):
        """Route transitions to their channel, one message per same-direction batch.

        Room and compute transitions never share a message, and each carries
        only its own dashboard. Within a channel, alerts and recoveries never
        share a message; conditions crossing in the same poll do.
        """
        room, compute = [], []
        for transition in transitions:
            key = transition.key
            if key == SUITE_TEMPERATURE_KEY or key.startswith("sensor_unavailable:"):
                room.append(transition)
            elif key.startswith(("gpu_temperature:", "machine_unavailable:")):
                compute.append(transition)
            else:
                self._log("no Slack audience for transition %s; dropping it", key)

        return self._post_batches(
            self.room_channel_id, "room", room, room_dashboard, dashboard_url
        ) + self._post_batches(
            self.compute_channel_id, "compute", compute, gpu_dashboard, dashboard_url
        )

    def _post_batches(self, channel_id, audience, transitions, dashboard, dashboard_url):
        if transitions and not channel_id:
            self._log(
                "no Slack %s channel configured; dropping %d transition(s)",
                audience,
                len(transitions),
            )
            return 0
        alerts = [t for t in transitions if t.kind is TransitionKind.ALERT]
        recoveries = [t for t in transitions if t.kind is TransitionKind.RECOVERY]
        posted = 0
        for batch in (alerts, recoveries):
            if not batch:
                continue
            headlines = [t.headline for t in batch]
            if self.post(channel_id, render_message(dashboard, headlines, dashboard_url)):
                posted += 1
        return posted

    def _log(self, message, *args):
        if self.logger is not None:
            self.logger.warning(message, *args)


def build_web_client(bot_token):
    """Create a Slack ``WebClient``, or ``None`` when no token is configured."""
    if not bot_token:
        return None
    from slack_sdk import WebClient

    return WebClient(token=bot_token)


def build_app(service, settings, logger=None):
    """Build the Bolt app that serves ``/labstatus``, ``/roomstatus`` and ``/gpustatus``.

    Responses are ephemeral; only genuine state changes are posted publicly.
    """
    from slack_bolt import App

    app = App(token=settings.bot_token, logger=logger)

    def reply(respond, name, render):
        try:
            text = render(max_age=COMMAND_MAX_AGE_SECONDS)
        except Exception as exc:
            if logger is not None:
                logger.exception("%s failed", name)
            respond(
                response_type="ephemeral",
                text=":x: LabMonitor could not build a status right now: `%s`" % exc,
            )
            return
        respond(response_type="ephemeral", text=text)

    @app.command("/labstatus")
    def handle_labstatus(ack, respond):
        ack()
        reply(respond, "/labstatus", service.dashboard_message)

    @app.command("/roomstatus")
    def handle_roomstatus(ack, respond):
        ack()
        reply(respond, "/roomstatus", service.room_dashboard_message)

    @app.command("/gpustatus")
    def handle_gpustatus(ack, respond):
        ack()
        reply(respond, "/gpustatus", service.gpu_dashboard_message)

    return app


def run_socket_mode(app, app_token, stop_event, handler_factory=None):
    """Connect the app to Slack over Socket Mode and block until stopped.

    Deliberately not ``SocketModeHandler.start()``: that waits on an event it
    owns privately, so nothing short of the process dying can wake it and
    systemd ends up sending SIGKILL after its timeout. Waiting on a
    caller-owned ``stop_event`` instead lets the signal handler return the
    main thread here, and ``close()`` always runs on the way out.
    """
    if handler_factory is None:
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        handler_factory = SocketModeHandler

    handler = handler_factory(app, app_token)
    try:
        handler.connect()
        stop_event.wait()
    finally:
        handler.close()
