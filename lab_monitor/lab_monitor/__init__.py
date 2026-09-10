"""LabMonitor: lab environment and GPU monitoring for the BioE 3201 suite.

Netdata owns telemetry, history and the detailed web dashboard. LabMonitor
owns only the parts Netdata cannot: the physical topology (which machine is in
which room), BLE room sensors, a compact current-state dashboard for Slack,
and one notification per genuine state change.
"""

__version__ = "1.0.0"
