"""Threshold state machine, transition detection and persistence.

Each condition is a two-state machine, and nothing else produces Slack traffic:

    NORMAL --(abnormal for trigger_after_seconds)--> ALERT
    ALERT  --(normal for recover_after_seconds)----> NORMAL

Conditions also accept a third input, *unknown*, which holds the current state
without transitioning. A stale sensor makes its room's temperature unknown;
sensor staleness is a separate condition and is the one that reports.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass

from .models import ConditionState, SensorState, Transition, TransitionKind

STATE_VERSION = 1

_ALERT = "alert"
_NORMAL = "normal"

_WARN = ":warning:"
_OK = ":white_check_mark:"


def room_key(room_id):
    """Condition key for a room's temperature."""
    return "room_temperature:%s" % room_id


def gpu_key(machine_id, index):
    """Condition key for one GPU's temperature."""
    return "gpu_temperature:%s:%s" % (machine_id, index)


def machine_key(machine_id):
    """Condition key for a machine's availability."""
    return "machine_unavailable:%s" % machine_id


def sensor_key(sensor_id):
    """Condition key for an established sensor's availability."""
    return "sensor_unavailable:%s" % sensor_id


@dataclass
class _Condition:
    """One condition's evaluation for a single tick."""

    key: str
    desired: str | None
    abnormal: bool
    notify: bool
    trigger_after: int
    recover_after: int
    alert_headline: str
    recovery_headline: str


class Assessment:
    """What the engine concluded this tick.

    ``transitions`` goes to Slack; the predicates drive the renderer's ``(!)``
    markers, so display and notifications never disagree.
    """

    def __init__(self, abnormal, transitions):
        self._abnormal = frozenset(abnormal)
        self.transitions = tuple(transitions)

    @property
    def abnormal_keys(self):
        """Every condition key currently out of range."""
        return self._abnormal

    def room_temperature_abnormal(self, room_id):
        """True when this room is above its temperature threshold."""
        return room_key(room_id) in self._abnormal

    def gpu_temperature_abnormal(self, machine_id, index):
        """True when this GPU is above its temperature threshold."""
        return gpu_key(machine_id, index) in self._abnormal

    def machine_unavailable(self, machine_id):
        """True when this machine has no fresh Netdata data."""
        return machine_key(machine_id) in self._abnormal

    def sensor_unavailable(self, sensor_id):
        """True when this established sensor has gone quiet."""
        return sensor_key(sensor_id) in self._abnormal


#: Flags nothing; for rendering a dashboard without running the engine.
EMPTY_ASSESSMENT = Assessment((), ())


class AlertEngine:
    """Holds the committed state of every condition between polls."""

    def __init__(self, config, states=None):
        self.config = config
        self.states = {}
        if states:
            for key, raw in states.items():
                self.states[key] = ConditionState.from_json(raw)

    # -- persistence ------------------------------------------------------

    def dump(self):
        """Serialise committed condition states for the state file."""
        return {key: state.to_json() for key, state in self.states.items()}

    # -- evaluation -------------------------------------------------------

    def evaluate(self, snapshot):
        """Advance every condition by one tick and report what changed."""
        conditions = list(self._conditions(snapshot))
        abnormal = {c.key for c in conditions if c.abnormal}

        transitions = []
        for condition in conditions:
            transition = self._advance(condition, snapshot.taken_at)
            if transition is not None:
                transitions.append(transition)

        self._forget_candidates_for({c.key for c in conditions})
        self._prune()
        return Assessment(abnormal, transitions)

    def _forget_candidates_for(self, evaluated):
        """Discard in-flight debounce timers for conditions we could not judge.

        A violation cannot be called sustained across a gap where we saw
        nothing, so the candidate is dropped while the committed state stands.
        """
        for key, state in self.states.items():
            if key not in evaluated:
                state.pending = None
                state.pending_since = None

    def _state(self, key):
        state = self.states.get(key)
        if state is None:
            state = ConditionState()
            self.states[key] = state
        return state

    def _advance(self, condition, now):
        """Apply the debounce rules to one condition, committing if due.

        A delay of zero commits on the same tick, which is what availability
        wants: its timeout is already the debounce.
        """
        state = self._state(condition.key)

        if condition.desired is None or condition.desired == state.state:
            state.pending = None
            state.pending_since = None
            return None

        if state.pending != condition.desired:
            state.pending = condition.desired
            state.pending_since = now

        since = state.pending_since if state.pending_since is not None else now
        delay = (
            condition.trigger_after
            if condition.desired == _ALERT
            else condition.recover_after
        )
        if (now - since) < delay:
            return None

        state.state = condition.desired
        state.pending = None
        state.pending_since = None

        if not condition.notify:
            return None
        if condition.desired == _ALERT:
            return Transition(condition.key, TransitionKind.ALERT, condition.alert_headline)
        return Transition(condition.key, TransitionKind.RECOVERY, condition.recovery_headline)

    def _prune(self):
        """Forget conditions whose subject has left the configuration."""
        live = set()
        for room in self.config.rooms:
            live.add(room_key(room.id))
        for machine in self.config.machines:
            live.add(machine_key(machine.id))
        for sensor in self.config.sensors:
            live.add(sensor_key(sensor.id))
        for key in list(self.states):
            if key.startswith("gpu_temperature:"):
                parts = key.split(":")
                machine_id = parts[1] if len(parts) > 2 else None
                if self.config.machine(machine_id) is None:
                    del self.states[key]
            elif key not in live:
                del self.states[key]

    # -- condition builders ----------------------------------------------

    def _conditions(self, snapshot):
        yield from self._machine_conditions(snapshot)
        yield from self._sensor_conditions(snapshot)
        yield from self._room_conditions(snapshot)
        yield from self._gpu_conditions(snapshot)

    def _machine_conditions(self, snapshot):
        availability = self.config.availability
        for machine in self.config.machines:
            reading = snapshot.machine(machine.id)
            if reading is None:
                continue
            key = machine_key(machine.id)
            unavailable = not reading.available
            yield _Condition(
                key=key,
                desired=_ALERT if unavailable else _NORMAL,
                abnormal=unavailable,
                notify=availability.alert_on_machine_unavailable,
                trigger_after=availability.trigger_after_seconds,
                recover_after=availability.recover_after_seconds,
                alert_headline="%s %s is unavailable — no fresh metrics in Netdata."
                % (_WARN, machine.name),
                recovery_headline="%s %s is reporting again." % (_OK, machine.name),
            )

    def _sensor_conditions(self, snapshot):
        """Availability conditions for sensors that have reported at least once.

        Never-seen sensors yield no condition, so a fresh install stays quiet.
        Sensors in the post-restart grace period yield no desired state, so a
        restart neither re-alerts a dead sensor nor recovers a silent one.
        """
        availability = self.config.availability
        for sensor in self.config.sensors:
            reading = snapshot.sensor(sensor.id)
            if reading is None or reading.state is SensorState.NEVER_SEEN:
                continue
            stale = reading.state is SensorState.STALE
            pending = reading.state is SensorState.PENDING
            label = self._sensor_label(sensor)
            yield _Condition(
                key=sensor_key(sensor.id),
                desired=None if pending else (_ALERT if stale else _NORMAL),
                abnormal=stale,
                notify=availability.alert_on_sensor_unavailable,
                trigger_after=availability.trigger_after_seconds,
                recover_after=availability.recover_after_seconds,
                alert_headline="%s %s sensor is unavailable — no BLE readings." % (_WARN, label),
                recovery_headline="%s %s sensor is reporting again." % (_OK, label),
            )

    def _room_conditions(self, snapshot):
        threshold = self.config.threshold("room_temperature")
        if threshold is None or not threshold.enabled:
            return
        for room in self.config.rooms:
            celsius = self._room_temperature(room.id, snapshot)
            if celsius is None:
                continue        # unknown: hold state, let sensor availability speak
            key = room_key(room.id)
            alerting = self._state(key).state == _ALERT
            abnormal = threshold.is_abnormal_c(celsius, alerting)
            yield _Condition(
                key=key,
                desired=_ALERT if abnormal else _NORMAL,
                abnormal=abnormal,
                notify=True,
                trigger_after=threshold.trigger_after_seconds,
                recover_after=threshold.recover_after_seconds,
                alert_headline="%s %s temperature crossed %s."
                % (_WARN, room.name, _limit_text(threshold)),
                recovery_headline="%s %s temperature returned to normal."
                % (_OK, room.name),
            )

    def _gpu_conditions(self, snapshot):
        threshold = self.config.threshold("gpu_temperature")
        if threshold is None or not threshold.enabled:
            return
        for machine in self.config.machines:
            reading = snapshot.machine(machine.id)
            if reading is None or not reading.available:
                continue
            for gpu in reading.gpus:
                if gpu.temperature_c is None:
                    continue
                key = gpu_key(machine.id, gpu.index)
                alerting = self._state(key).state == _ALERT
                abnormal = threshold.is_abnormal_c(gpu.temperature_c, alerting)
                yield _Condition(
                    key=key,
                    desired=_ALERT if abnormal else _NORMAL,
                    abnormal=abnormal,
                    notify=True,
                    trigger_after=threshold.trigger_after_seconds,
                    recover_after=threshold.recover_after_seconds,
                    alert_headline="%s %s %s temperature crossed %s."
                    % (_WARN, machine.name, gpu.label, _limit_text(threshold)),
                    recovery_headline="%s %s %s temperature returned to normal."
                    % (_OK, machine.name, gpu.label),
                )

    def _room_temperature(self, room_id, snapshot):
        """The room's current temperature, or ``None`` if unknown.

        With several sensors in a room the hottest live reading wins.
        """
        best = None
        for sensor in self.config.sensors_in(room_id):
            reading = snapshot.sensor(sensor.id)
            if reading is None or reading.state is not SensorState.OK:
                continue
            if reading.temperature_c is None:
                continue
            if best is None or reading.temperature_c > best:
                best = reading.temperature_c
        return best

    def _sensor_label(self, sensor):
        room = self.config.room(sensor.room)
        if room is None:
            return sensor.name
        if len(self.config.sensors_in(sensor.room)) == 1:
            return room.name
        return "%s %s" % (room.name, sensor.name)


def _limit_text(threshold):
    """Render a threshold for a headline, e.g. ``82°F``."""
    value = threshold.high
    text = ("%.1f" % value).rstrip("0").rstrip(".")
    return "%s°%s" % (text, threshold.unit)


# -- state file ------------------------------------------------------------


def load_state(path):
    """Read the persisted state document, returning ``{}`` if unusable.

    A corrupt file costs one round of re-alerting, never a failed start.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_state(path, conditions, sensors):
    """Write the state document atomically (temp file in-dir, then rename).

    Contains timestamps and state names only. No tokens, no addresses.
    """
    document = {
        "version": STATE_VERSION,
        "conditions": conditions,
        "sensors": sensors,
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".lab_monitor_state.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
