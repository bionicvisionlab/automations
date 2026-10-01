# LabMonitor

Lab environment and GPU monitoring for the BioE 3201 suite.

Netdata owns telemetry, history and the detailed web dashboard. LabMonitor adds
the physical topology, Govee BLE room sensors, a compact Slack dashboard, and
one notification per genuine state change.

One workstation acts as the **Parent**: it receives the other machines'
metrics, collects its own, hosts the BLE receiver, and runs the LabMonitor
service. The other two are Netdata **Children**. Substitute your own hostnames
throughout — the topology lives in `lab_monitor.toml`, not in the code.

```text
GPU workstation 1 ─ Netdata child ─┐
GPU workstation 2 ─ Netdata child ─┼──► Parent
Parent ──────────── Netdata local ─┘         │
                                             ├── Netdata web dashboard
Govee H5075s ─ BLE ─► LabMonitor ─► Netdata  │
                            │                │
                            └─ current state ┘
                                     │
                                     ├── /labstatus, /roomstatus, /gpustatus
                                     ├── room alerts    ─► room channel
                                     └── compute alerts ─► compute channel
```

## Output

```text
BioE 3201                                         3:42 PM
─────────────────────────────────────────────────────────

ENVIRONMENT
BioE 3201A  81.2°F      41%
BioE 3201B  84.7°F (!)  39%
BioE 3201C      --           not yet seen
BioE 3201D  79.8°F      42%
Foyer       80.1°F      40%

COMPUTE
BioE 3201A
  gpu2         GPU0  73°C   96%  fan 71%  382W  18.2/24GB

BioE 3201D
  gpu3         unavailable (!)

Foyer
  DeepThought  GPU0  78°C   99%  fan 82%  427W  21.1/24GB
```

`(!)` marks a value currently outside its configured range.

That is `/labstatus`. `/roomstatus` shows the same header with only
`ENVIRONMENT`; `/gpustatus` only `COMPUTE` (machine availability included).
All three reply ephemerally.

## Alerting

Each condition is a two-state machine:

```text
NORMAL ──(abnormal for trigger_after_seconds)──► ALERT
ALERT  ──(normal for recover_after_seconds)────► NORMAL
```

- A crossing counts only once it has lasted `trigger_after_seconds`.
- Recovery is measured against `high - recovery_margin`, so a value hovering at
  the limit cannot flap.
- Entering an abnormal state posts one message with the relevant half of the
  dashboard. Staying abnormal posts nothing further — there are no "still hot"
  reminders.
- Recovering posts one message, also with the relevant half of the dashboard.
- Each GPU, machine and sensor alerts on its own. Rooms don't (see below).
- Alert state persists atomically across restarts.

### Where alerts go

| Transition | Channel | Dashboard shown |
| ---------- | ------- | --------------- |
| Suite room temperature | room | `ENVIRONMENT` |
| Sensor unavailable / back | room | `ENVIRONMENT` |
| GPU temperature | compute | `COMPUTE` |
| Machine unavailable / back | compute | `COMPUTE` |
| Hot-weather advisory (daily, not a transition) | room | none |

Transitions crossing in the same poll share a message per channel and
direction: a hot room and a hot GPU on one tick post once to each channel,
never one combined message. Alerts and recoveries are never mixed.

Alerting conditions: room temperature, GPU temperature, machine availability,
established sensor availability. Fan speed, utilization, power and VRAM are
displayed but never alert — a GPU at 99% is usually *why* the room is warm.

### Room temperature

Slack gets one alert for the whole suite, not one per room:

- Alert when the first room has been above 82°F for 15 minutes. It lists every
  room that has reached that state.
- Rooms heating up or cooling down after that post nothing. `/labstatus` and
  `/roomstatus` still flag each room.
- Recovery posts once, when all alerting rooms have recovered (at or below 80°F
  for 10 minutes).
- A room with a dead sensor keeps its last state.
- A room with `temperature_alerts = false` in its `[[rooms]]` entry never
  opens, appears in, or prolongs a suite alert. It is still monitored,
  displayed, logged and flagged `(!)`.

82°F for 15 minutes follows the university's heat-safety guidance.

### Hot-weather advisory

With `[weather] enabled = true`, the first poll after 7 AM (the Parent's local
time) asks the National Weather Service for today's daytime high at the
configured coordinates. If the high is at or above `notify_high`, one advisory
goes to the room channel:

```text
Hot day expected: NWS forecasts a high of 88°F at UCSB today. Warmest indoor
reading right now: 79.3°F in BioE 3201B. Consider working from home today if
you can.
```

The indoor sentence names the hottest live sensor and is left out when no
sensor has a current reading. This is not an alert condition: it has no
debounce, no recovery and no dashboard, and it posts at most once per day.
The date of the last completed check is saved in the state file, so a restart
does not repeat that day's advisory. A failed NWS request, or a refused Slack
post, completes nothing: it is logged and retried every 10 minutes. After
the evening forecast update there is no daytime period left for today, so a
check that first succeeds that late completes without posting.

```toml
[weather]
enabled = true
latitude = 34.41305      # BioEngineering Building, UCSB
longitude = -119.84487
notify_high = 82.0
unit = "F"
```

## Sensor states

| State | Dashboard | Alerts |
| ----- | --------- | ------ |
| never seen | `-- not yet seen` | never |
| awaiting | `-- awaiting reading` | never (post-restart grace period) |
| ok | `81.2°F  41%` | on temperature threshold |
| stale | `-- unavailable (!)` | off by default; `alert_on_sensor_unavailable = true` alerts once, then silence |

A stale sensor reports no value; its last reading is never presented as
current. The system runs correctly with zero Govee sensors present.

## Telemetry log

With `[logging] path` set, each polling cycle appends one CSV row of **raw
measurements** -- room temperature/humidity/battery and per-GPU
temperature/utilization/fan/power/VRAM, as they were read in raw units
(Celsius, bytes -- display units never reach the log):

```toml
[logging]
path = "/var/lib/bvl-automations/lab_monitor.csv"
```

This is not a record of alerts, status or anything else LabMonitor derives:
no thresholds, no transitions, no aggregation. It exists so the numbers can be
re-analysed offline without going through Netdata. Omit `path` to turn it off.

Anything not known at that moment is an empty cell, never the previous
reading -- a stale sensor, an unavailable machine and a metric the driver did
not report all write blanks, so "23.5 C" stays distinguishable from "we did
not know".

Govee cells follow the **broadcast**, not the poll. A reading counts as current
for up to `sensor_timeout_seconds` after it arrives, so the dashboard keeps
showing it; the log writes it in one row and blanks the sensor until it
broadcasts again. Ten minutes of blanks below one temperature means one
measurement was taken, not that twenty were. GPU cells are read fresh from
Netdata each poll, so they are written every row.

The header is fixed per file, and the configured path always holds the current
schema. If the configured sensors or machines change, or a new GPU appears, the
old file is moved aside to `lab_monitor-20260910T154200.csv` and the new schema
starts at `lab_monitor.csv` -- so a restart resumes the current file instead of
finding a superseded header. Nothing rotates by size or age.

---

# Deployment

## 1. Netdata on all three machines

```bash
wget -O /tmp/netdata-kickstart.sh https://get.netdata.cloud/kickstart.sh
sudo sh /tmp/netdata-kickstart.sh --stable-channel --disable-telemetry
```

Set each hostname to match its `netdata_hostname` in the TOML:

```bash
sudo hostnamectl set-hostname <hostname>
sudo systemctl restart netdata
curl -s localhost:19999/api/v3/nodes | python3 -m json.tool | grep '"nm"'
```

The `nvidia_smi` collector is **disabled by default** (`go.d.conf` ships
`nvidia_smi: no`) and does not auto-detect. Enable it explicitly on each
machine:

```bash
sudo /etc/netdata/edit-config go.d.conf        # set: nvidia_smi: yes
sudo /etc/netdata/edit-config go.d/nvidia_smi.conf
sudo systemctl restart netdata
```

The stock `go.d/nvidia_smi.conf` already defines a job, so it usually needs no
edit; set `binary_path` if `nvidia-smi` is not on `PATH`. Then confirm the
contexts exist:

```bash
curl -s localhost:19999/api/v3/contexts | grep -o 'nvidia_smi\.[a-z_]*' | sort -u
```

Expect `gpu_temperature`, `gpu_utilization`, `gpu_fan_speed_perc`,
`gpu_power_draw`, `gpu_frame_buffer_memory_usage`. If empty, run the collector
directly and confirm the `netdata` user can reach the GPUs:

```bash
sudo -u netdata /usr/libexec/netdata/plugins.d/go.d.plugin -d -m nvidia_smi
sudo -u netdata nvidia-smi
```

Multiple GPUs need no configuration: each card is an instance carrying an
`index` label, and LabMonitor groups on it.

The collector defaults to `update_every: 10`, so GPU values can be up to ten
seconds old. Lower it in `go.d/nvidia_smi.conf` for finer resolution.

**Verify VRAM once per card model.** Netdata exposes no total for the frame
buffer, so LabMonitor sums `free + used + reserved`. Confirm that matches
reality before trusting the number:

```bash
nvidia-smi --query-gpu=index,memory.total --format=csv
```

Compare against the `/totalGB` figure on the dashboard.

## 2. Streaming

Generate the API key on the Parent. It is a secret and is not in this repo:

```bash
uuidgen
```

**Parent** — `sudo /etc/netdata/edit-config stream.conf`, using
`netdata/parent-stream.conf.example`. Set `[stream] enabled = no`, add a
section named after the UUID with `enabled = yes`, and restrict `allow from`
to the children's static IPs.

Retention and binding, via `sudo /etc/netdata/edit-config netdata.conf`:

```ini
[db]
    mode = dbengine
    storage tiers = 3
    dbengine tier 0 retention size = 2GiB
    dbengine tier 1 retention size = 2GiB
    dbengine tier 2 retention size = 2GiB

[web]
    bind to = 127.0.0.1 <parent lab IP>
```

Keep Netdata off the public internet; reach it over VPN or an SSH tunnel.

**Children** — same file, using `netdata/child-stream.conf.example`:
`enabled = yes`, `destination = <parent IP>:19999`, and the same `api key`.

Restart Netdata on all three, then verify:

```bash
sudo journalctl -u netdata -n 50 | grep -i stream               # on a child
curl -s localhost:19999/api/v3/nodes | python3 -m json.tool \
    | grep -E '"nm"|"state"'                                    # on the Parent
```

All three hostnames should appear.

## 3. StatsD receiver (Parent)

```bash
sudo mkdir -p /etc/netdata/statsd.d
sudo cp netdata/statsd-labmonitor.conf.example /etc/netdata/statsd.d/labmonitor.conf
sudo systemctl restart netdata
```

The chart definitions use patterns, so new rooms and sensors need no change
here.

## 4. LabMonitor (Parent)

```bash
cd /etc/bvl-automations && sudo git pull

sudo apt install -y python3-venv
sudo python3 -m venv /opt/bvl-automations/lab-monitor
sudo /opt/bvl-automations/lab-monitor/bin/pip install \
    '/etc/bvl-automations/lab_monitor[slack,ble]'
```

Drop `,ble` if the Govee sensors are not installed yet.

Create the service user before anything runs as it:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin labmonitor
```

```bash
sudo mkdir -p /etc/bvl-automations
sudo cp lab_monitor/config.example.toml /etc/bvl-automations/lab_monitor.toml
sudo cp lab_monitor/lab_monitor.conf.example /etc/bvl-automations/.lab_monitor.conf
sudo chmod 600 /etc/bvl-automations/.lab_monitor.conf
sudo editor /etc/bvl-automations/lab_monitor.toml      # rooms, machines
sudo editor /etc/bvl-automations/.lab_monitor.conf     # Slack tokens, URLs
```

Validate before starting. `check-config` reports what the configuration
resolved to and what is missing; `status` prints the dashboard once without
writing any state. Load the env file the way systemd does rather than
expanding it into arguments — it is `chmod 600` precisely so its contents stay
out of the process table:

```bash
sudo systemd-run --pipe --quiet --uid=labmonitor \
    --property=EnvironmentFile=/etc/bvl-automations/.lab_monitor.conf \
    /opt/bvl-automations/lab-monitor/bin/python -m lab_monitor check-config
```

Swap `check-config` for `status` to render the dashboard. This reuses the same
`EnvironmentFile` as the unit, so it validates what the service will actually
see.

```bash
sudo cp lab_monitor/systemd/lab-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lab-monitor
sudo journalctl -u lab-monitor -f
```

## 5. Slack app

LabMonitor needs its own Slack app: Socket Mode binds one app-level token to
one process, so it cannot share DeadlineWatcher's.

1. <https://api.slack.com/apps> → **Create New App** → **From scratch**, named
   **LabMonitor**
2. **Socket Mode** → enable. Create an App-Level Token with `connections:write`
   → `LAB_MONITOR_SLACK_APP_TOKEN` (`xapp-…`)
3. **Slash Commands** → **Create New Command**, three times. No Request URL —
   Socket Mode does not use one.

   | Command | Description |
   | ------- | ----------- |
   | `/labstatus` | `Show lab temperatures and GPU status` |
   | `/roomstatus` | `Show room temperatures and humidity` |
   | `/gpustatus` | `Show GPU and workstation status` |

4. **OAuth & Permissions** → Bot Token Scopes: `commands`, `chat:write`
5. **Install to Workspace** → Bot User OAuth Token (`xoxb-…`) →
   `LAB_MONITOR_SLACK_BOT_TOKEN`
6. `/invite @LabMonitor` in both notification channels and record their IDs
   (channel name → **About**):
   - room/environment alerts, for everyone in the suite (we use `#general`) →
     `LAB_MONITOR_SLACK_ROOM_CHANNEL_ID`
   - GPU/workstation alerts, for the machines' users (we use `#deepthought`) →
     `LAB_MONITOR_SLACK_COMPUTE_CHANNEL_ID`

The legacy `LAB_MONITOR_SLACK_CHANNEL_ID` is a fallback for either channel
that is unset, so an existing single-channel deployment keeps posting both
kinds of alert there (each still with only its own dashboard half) until the
new variables are added. A missing channel drops that kind of alert with a
log warning; slash commands need only the two tokens.

The commands reply ephemerally. Only state changes are posted publicly.

## 6. Adding Govee H5075 sensors

```bash
sudo /opt/bvl-automations/lab-monitor/bin/python -m lab_monitor discover-govee
```

This runs an active BLE scan — the H5075 is only identifiable from the scan
response — and prints each device's address, current readings, and a ready-made
`[[sensors]]` block. Add them to the TOML, assign rooms, and
`sudo systemctl restart lab-monitor`. No code changes are needed.

---

# Development

```bash
cd lab_monitor && python -m pytest tests/ -q
```

Tests need no GPU, Netdata, Bluetooth, Slack or real clock.

| File | Responsibility |
| ---- | -------------- |
| `config.py` | TOML parsing, validation, environment resolution |
| `models.py` | Plain data: topology, readings, snapshots, thresholds |
| `netdata.py` | `/api/v3/data` client, json2 parsing, StatsD emitter |
| `govee.py` | `SensorStore` (pure logic), `decode_h5075`, `GoveeReceiver` (BLE adapter) |
| `status.py` | Snapshot assembly and the text dashboards (full, room, GPU) |
| `csvlog.py` | Raw per-poll telemetry as a CSV append log |
| `alerts.py` | Transition state machine and atomic persistence |
| `slack.py` | Status commands and transition routing/posting |
| `weather.py` | NWS forecast client and the once-daily hot-weather advisory |
| `__main__.py` | `Service` composition and the CLI |

Govee-specific code is confined to `decode_h5075` and `GoveeReceiver`;
everything else speaks in `SensorReading`s. `bleak` and `slack_bolt` are
imported lazily. The only Govee model supported is the H5075, whose
advertisement is decoded directly — `bleak` is the sole BLE dependency.

## Netdata API assumptions

Read from the current OpenAPI specification and collector source, but **not
yet exercised against a running Parent**. Anything wrong here surfaces as a
missing or implausible dashboard value.

- `/api/v3/data` with `format=json2`. Deprecated chart-discovery endpoints are
  not used.
- `result.labels` lists dimension ids with `"time"` first; `result.point` maps
  index positions (`value`, `arp`, `pa`); bit 0 of `pa` means EMPTY, which is
  how a missing metric is detected rather than read as zero.
- `scope_nodes` matches hostname, node id or machine GUID.
- `group_by=label` with `group_by_label=index` yields one dimension per GPU.
- `time_group` averages *within* each output point, so GPU queries ask for one
  point per second over a 60-second window and take the newest non-empty one.
  A single point over a wider window would report a mean as a current value.
- `db.last_entry` is the newest timestamp actually held for the queried
  metrics, and is what decides availability.
- `options` is one string, split on `,`, space or `|`.
- The frame buffer context exposes `free`/`used`/`reserved` and no total, so
  total VRAM is their sum. Confirm against `nvidia-smi` per card model.
