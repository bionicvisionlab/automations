"""Slack surface: the ``/labstatus`` command and transition notifications.

LabMonitor needs its own Slack app: Socket Mode binds one app-level token to
one process, so it cannot share DeadlineWatcher's. ``slack_bolt`` is imported
lazily so the package works without it installed.
"""

from __future__ import annotations

from .models import TransitionKind
from .status import render_message

#: How stale a cached snapshot may be before ``/labstatus`` forces a refresh.
COMMAND_MAX_AGE_SECONDS = 20.0


class SlackNotifier:
    """Posts transition messages to the configured channel.

    Failures are logged and swallowed; a Slack outage must not stop monitoring.
    """

    def __init__(self, client, channel_id, logger=None):
        self.client = client
        self.channel_id = channel_id
        self.logger = logger

    @property
    def enabled(self):
        """True when we have somewhere to post."""
        return bool(self.client and self.channel_id)

    def post(self, text):
        """Post one message. Returns True if Slack accepted it."""
        if not self.enabled:
            self._log("no Slack channel configured; dropping message")
            return False
        try:
            self.client.chat_postMessage(channel=self.channel_id, text=text)
            return True
        except Exception as exc:
            self._log("could not post to Slack: %s", exc)
            return False

    def post_transitions(self, transitions, dashboard, dashboard_url=None):
        """Post one message per batch of same-direction transitions.

        Alerts and recoveries never share a message; conditions crossing in the
        same poll do, rather than repeating an identical dashboard.
        """
        alerts = [t for t in transitions if t.kind is TransitionKind.ALERT]
        recoveries = [t for t in transitions if t.kind is TransitionKind.RECOVERY]
        posted = 0
        for batch in (alerts, recoveries):
            if not batch:
                continue
            headlines = [t.headline for t in batch]
            if self.post(render_message(dashboard, headlines, dashboard_url)):
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
    """Build the Bolt app that serves ``/labstatus``.

    Responses are ephemeral; only genuine state changes are posted publicly.
    """
    from slack_bolt import App

    app = App(token=settings.bot_token, logger=logger)

    @app.command("/labstatus")
    def handle_labstatus(ack, respond, command):
        ack()
        try:
            text = service.dashboard_message(max_age=COMMAND_MAX_AGE_SECONDS)
        except Exception as exc:
            if logger is not None:
                logger.exception("/labstatus failed")
            respond(
                response_type="ephemeral",
                text=":x: LabMonitor could not build a status right now: `%s`" % exc,
            )
            return
        respond(response_type="ephemeral", text=text)

    return app


def run_socket_mode(app, app_token):
    """Connect the app to Slack over Socket Mode and block."""
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    SocketModeHandler(app, app_token).start()
