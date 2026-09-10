# LabMonitor deployment runbook

This is the operator runbook for deploying, upgrading, and replacing the
LabMonitor installation. It records the path that has been exercised on the
BioE 3201 deployment rather than trying to automate every machine-specific
choice.

LabMonitor has three layers:

```text
GPU workstations -> Netdata Children -> Netdata Parent on DeepThought
DeepThought local GPU --------------------/
Govee H5075 BLE -> LabMonitor -> Netdata StatsD
LabMonitor -> Slack (/labstatus + transition alerts)
```

The **Parent** runs Netdata and LabMonitor. The other GPU workstations run only
Netdata and stream to the Parent.

## What is deliberately not automated

`install.sh` handles only deterministic LabMonitor setup. It does **not** edit
Netdata configuration, choose IP addresses, create firewall rules, generate
Slack tokens, rename hosts, or guess the physical room topology. Those choices
are explicit below so a future operator can inspect them.

Commands assume the repository checkout is:

```text
/etc/bvl-automations
```

and the LabMonitor virtual environment is:

```text
/opt/bvl-automations/lab-monitor
```

The package requires Python >= 3.11. Ubuntu 20.04/Focal's system Python 3.8 is
not sufficient.

---

# 1. Parent prerequisites

On the intended Parent:

```bash
cd /etc/bvl-automations
git branch --show-current
git status --short
git log -1 --oneline

hostnamectl --static
nvidia-smi
```

Expected:

- the deployment checkout is on `master` and clean;
- the NVIDIA driver works before Netdata is involved;
- the hostname is sensible. There is no need to rename the OS host just to
  satisfy LabMonitor: `netdata_hostname` in `lab_monitor.toml` must match the
  name Netdata actually reports.

If `/etc/bvl-automations` does not exist yet, clone the repository there before
continuing.

---

# 2. Install LabMonitor's Python environment

The preferred path is the repository installer:

```bash
cd /etc/bvl-automations
sudo ./lab_monitor/install.sh
```

The installer:

- requires the checkout to be `/etc/bvl-automations`;
- installs `uv` if needed;
- installs a managed Python 3.11 under `/opt/bvl-automations/python`;
- creates/reuses `/opt/bvl-automations/lab-monitor`;
- installs LabMonitor with Slack and BLE extras;
- creates the `labmonitor` system user if needed;
- copies example config files only when live files do not already exist;
- installs the systemd unit and runs `daemon-reload`;
- does **not** start the service.

Verify:

```bash
/opt/bvl-automations/lab-monitor/bin/python --version
/opt/bvl-automations/lab-monitor/bin/python -c \
  'import lab_monitor; print("LabMonitor import OK")'
```

Expected: Python 3.11.x or newer and `LabMonitor import OK`.

## Manual Python fallback

If the installer itself needs debugging, this is the Python setup that was
used successfully on Ubuntu 20.04:

```bash
sudo rm -rf /opt/bvl-automations/lab-monitor

curl -LsSf https://astral.sh/uv/install.sh \
  | sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

sudo mkdir -p /opt/bvl-automations/python
sudo env UV_PYTHON_INSTALL_DIR=/opt/bvl-automations/python \
  /usr/local/bin/uv python install 3.11

sudo env UV_PYTHON_INSTALL_DIR=/opt/bvl-automations/python \
  /usr/local/bin/uv venv --python 3.11 --seed \
  /opt/bvl-automations/lab-monitor

sudo /opt/bvl-automations/lab-monitor/bin/pip install \
  '/etc/bvl-automations/lab_monitor[slack,ble]'
```

Do not try to fix a Python 3.8 environment by upgrading `pip`; the interpreter
version itself is the incompatibility.

---

# 3. Install Netdata on every GPU machine

Run on the Parent and each Child if Netdata is not already installed:

```bash
wget -O /tmp/netdata-kickstart.sh https://get.netdata.cloud/kickstart.sh
sudo sh /tmp/netdata-kickstart.sh --stable-channel --disable-telemetry
```

Verify:

```bash
systemctl status netdata --no-pager
curl -s http://127.0.0.1:19999/api/v3/nodes | python3 -m json.tool
```

Record the Parent's `"nm"` value. That exact string is the Parent machine's
`netdata_hostname` in `lab_monitor.toml`.

> The commands below assume the native-package config path `/etc/netdata`.
> Adjust if a different Netdata installation uses another config root.

---

# 4. Enable NVIDIA collection on every GPU machine

First verify the `netdata` user can reach the GPU:

```bash
sudo -u netdata nvidia-smi
```

The `nvidia_smi` collector is disabled by default and does not auto-detect.
Enable it:

```bash
sudo /etc/netdata/edit-config go.d.conf
```

Set:

```yaml
modules:
  nvidia_smi: yes
```

The stock job is normally sufficient:

```bash
sudo /etc/netdata/edit-config go.d/nvidia_smi.conf
```

Only set `binary_path` if `nvidia-smi` is not on the collector's `PATH`.
Restart and verify:

```bash
sudo systemctl restart netdata
sleep 12

curl -s http://127.0.0.1:19999/api/v3/contexts \
  | grep -o 'nvidia_smi\.[a-z_]*' \
  | sort -u
```

Expected contexts include:

```text
nvidia_smi.gpu_fan_speed_perc
nvidia_smi.gpu_frame_buffer_memory_usage
nvidia_smi.gpu_power_draw
nvidia_smi.gpu_temperature
nvidia_smi.gpu_utilization
```

If they do not appear:

```bash
sudo -u netdata /usr/libexec/netdata/plugins.d/go.d.plugin -d -m nvidia_smi
sudo -u netdata nvidia-smi
```

If that plugin path does not exist, locate it first:

```bash
sudo find /usr/libexec /usr/lib /opt/netdata -name go.d.plugin -type f 2>/dev/null
```

The collector defaults to roughly 10-second updates. LabMonitor intentionally
selects the newest non-empty sample rather than averaging a multi-minute window.

## Validate VRAM once per GPU model

Netdata exposes used/free/reserved frame-buffer values rather than a documented
`total`; LabMonitor computes the total. Compare it once against NVIDIA:

```bash
nvidia-smi --query-gpu=index,memory.total --format=csv
```

Later compare this with the `/totalGB` value shown by LabMonitor.

---

# 5. Configure the Netdata Parent

## 5.1 Identify the Parent's lab-network address

```bash
hostname -I
```

Choose the address reachable by the Child workstations. Call it
`<PARENT_IP>` below.

## 5.2 Generate the streaming key

On the Parent:

```bash
uuidgen
```

This UUID is a secret shared by the Parent and Children. Do not commit it.
Call it `<STREAM_KEY>` below.

## 5.3 Parent `stream.conf`

Edit:

```bash
sudo /etc/netdata/edit-config stream.conf
```

Use:

```ini
[stream]
    enabled = no

[<STREAM_KEY>]
    enabled = yes
    type = api
    allow from = <CHILD1_IP> <CHILD2_IP>
    default memory mode = dbengine
    health enabled by default = auto
    default postpone alarms on connect seconds = 60
```

Use the actual static/reserved Child addresses. Do not leave `allow from = *`
on a routable interface.

## 5.4 Parent database and web binding

Edit:

```bash
sudo /etc/netdata/edit-config netdata.conf
```

The commissioned deployment uses dbengine storage and binds the dashboard to
localhost plus the Parent's lab-network interface:

```ini
[db]
    mode = dbengine
    storage tiers = 3
    dbengine tier 0 retention size = 2GiB
    dbengine tier 1 retention size = 2GiB
    dbengine tier 2 retention size = 2GiB

[web]
    bind to = 127.0.0.1 <PARENT_IP>
```

Restart and verify the listener:

```bash
sudo systemctl restart netdata
sudo ss -ltnp | grep 19999
```

Expected shape:

```text
LISTEN ... <PARENT_IP>:19999 ... netdata
LISTEN ... 127.0.0.1:19999 ... netdata
```

Then verify both addresses locally:

```bash
curl -I http://127.0.0.1:19999
curl -I http://<PARENT_IP>:19999
```

Do not continue to firewall debugging until both local requests work.

---

# 6. Firewall access to port 19999

Port 19999 serves both Netdata's web/API endpoint and Parent/Child streaming.
Keep it restricted.

## 6.1 Children

On the Parent, allow each Child explicitly:

```bash
sudo ufw allow from <CHILD1_IP> to any port 19999 proto tcp
sudo ufw allow from <CHILD2_IP> to any port 19999 proto tcp
```

## 6.2 Human dashboard access

If the admin machine has a stable address, allow that address. If it uses DHCP,
allow the smallest trusted subnet that contains it:

```bash
sudo ufw allow from <TRUSTED_ADMIN_SUBNET> to any port 19999 proto tcp
```

At UCSB commissioning, a client on `169.231.178.x` was in
`169.231.128.0/18`; allowing that subnet made the dashboard reachable. Treat
that as a site-specific example, not a universal requirement.

Do **not** use:

```bash
sudo ufw allow 19999
```

unless deliberately exposing Netdata much more broadly.

Verify:

```bash
sudo ufw status
```

From a Windows admin machine:

```powershell
Test-NetConnection <PARENT_IP> -Port 19999
```

Expected:

```text
TcpTestSucceeded : True
```

If local `curl` works, UFW allows the client/subnet, but this test still fails,
an upstream network ACL/firewall is the next suspect.

## 6.3 SSH-tunnel alternative

To avoid opening dashboard access at all:

```bash
ssh -L 19999:localhost:19999 <user>@<PARENT_IP>
```

Then browse to:

```text
http://localhost:19999
```

## 6.4 Netdata Cloud login is not part of this deployment

LabMonitor depends only on the local Agent API. A Netdata Cloud account is not
required for this architecture.

If the local dashboard unexpectedly forces Cloud authentication, inspect the
web access settings rather than adding a Cloud dependency. In particular:

```bash
grep -n 'bearer token protection' /etc/netdata/netdata.conf
```

For a UFW-restricted local dashboard, `bearer token protection = no` is the
intended setup. Restart Netdata after changing it. The older bundled dashboard
can also be reached at:

```text
http://<PARENT_IP>:19999/v1
```

---

# 7. Configure each Netdata Child

Repeat this section for each non-Parent GPU workstation.

First complete sections 3 and 4 locally: Netdata must run and the local NVIDIA
contexts must exist before streaming is debugged.

Record the Child's actual IP and Netdata node name:

```bash
hostname -I
curl -s http://127.0.0.1:19999/api/v3/nodes | python3 -m json.tool
```

Edit:

```bash
sudo /etc/netdata/edit-config stream.conf
```

Use:

```ini
[stream]
    enabled = yes
    destination = <PARENT_IP>:19999
    api key = <STREAM_KEY>
    buffer size bytes = 10485760
    reconnect delay seconds = 5
    timeout seconds = 60
```

Restart and inspect streaming:

```bash
sudo systemctl restart netdata
sudo journalctl -u netdata -n 100 --no-pager | grep -i stream
```

Back on the Parent:

```bash
curl -s http://127.0.0.1:19999/api/v3/nodes | python3 -m json.tool
```

Do not add the Child to LabMonitor until it appears here. Otherwise LabMonitor
will correctly report a configured-but-unreachable machine.

---

# 8. Install the Parent StatsD chart definition

On the Parent:

```bash
cd /etc/bvl-automations
sudo mkdir -p /etc/netdata/statsd.d
sudo cp lab_monitor/netdata/statsd-labmonitor.conf.example \
  /etc/netdata/statsd.d/labmonitor.conf
sudo systemctl restart netdata
```

This is used for Govee room history. It is harmless before any sensors exist.

---

# 9. Configure LabMonitor

`install.sh` creates live files from the examples only when they do not already
exist:

```text
/etc/bvl-automations/lab_monitor.toml
/etc/bvl-automations/.lab_monitor.conf
```

## 9.1 Topology and thresholds

Edit:

```bash
sudo editor /etc/bvl-automations/lab_monitor.toml
```

Keep the room IDs short and stable. Example:

```toml
[site]
name = "BioE 3201"

[[rooms]]
id = "a"
name = "BioE 3201A"

[[rooms]]
id = "b"
name = "BioE 3201B"
```

For each machine, `netdata_hostname` must exactly match the Parent's
`/api/v3/nodes` output:

```toml
[[machines]]
id = "deepthought"
name = "DeepThought"
room = "foyer"
netdata_hostname = "DeepThought"
parent = true
```

Several machines may share a room. Rooms may contain no machines.

If deploying the Parent before the Children, configure only the Parent at
first. Add each Child after Netdata streaming has been verified.

The default alert thresholds are intentionally explicit in TOML. Review rather
than blindly accepting them:

```toml
[thresholds.room_temperature]
high = 82.0
unit = "F"
trigger_after_seconds = 600
recovery_margin = 2.0

[thresholds.gpu_temperature]
high = 80.0
unit = "C"
trigger_after_seconds = 120
recovery_margin = 3.0
```

Govee `[[sensors]]` entries may be omitted entirely until sensors are present.

## 9.2 Environment/secrets

Edit:

```bash
sudo editor /etc/bvl-automations/.lab_monitor.conf
sudo chmod 600 /etc/bvl-automations/.lab_monitor.conf
```

Before Slack is configured, the file can contain only:

```ini
LAB_MONITOR_CONFIG=/etc/bvl-automations/lab_monitor.toml
NETDATA_URL=http://127.0.0.1:19999
```

Optionally add the browser-visible dashboard URL:

```ini
NETDATA_DASHBOARD_URL=http://<PARENT_IP>:19999
```

Do not leave fake `xoxb-...` or `xapp-...` placeholder values in the live
file; omit Slack variables until real credentials exist.

---

# 10. Validate LabMonitor before starting systemd

Run config validation using the same environment-file semantics as the service:

```bash
sudo systemd-run --pipe --quiet --uid=labmonitor \
  --property=EnvironmentFile=/etc/bvl-automations/.lab_monitor.conf \
  /opt/bvl-automations/lab-monitor/bin/python \
  -m lab_monitor check-config
```

Then render a one-shot dashboard without writing alert state:

```bash
sudo systemd-run --pipe --quiet --uid=labmonitor \
  --property=EnvironmentFile=/etc/bvl-automations/.lab_monitor.conf \
  /opt/bvl-automations/lab-monitor/bin/python \
  -m lab_monitor status
```

This is the most useful boundary between installation/configuration problems
and service/Slack problems.

Compare current GPU values with NVIDIA:

```bash
nvidia-smi \
  --query-gpu=index,temperature.gpu,utilization.gpu,fan.speed,power.draw,memory.used,memory.total \
  --format=csv
```

The values need not be byte-for-byte identical because Netdata samples on its
own cadence, but they should be physically plausible and close in time.

---

# 11. Start the LabMonitor service

The installer has already copied the unit and run `daemon-reload`.

The unit includes:

```text
User=labmonitor
Group=labmonitor
SupplementaryGroups=bluetooth
```

Check that the Bluetooth group exists before enabling the service:

```bash
getent group bluetooth
```

If it does not exist and BLE will be used, install/configure BlueZ before
commissioning the sensors. Do not add raw-socket capabilities; Bleak talks to
BlueZ over D-Bus.

Enable and start:

```bash
sudo systemctl enable --now lab-monitor
systemctl status lab-monitor --no-pager
sudo journalctl -u lab-monitor -n 100 --no-pager
```

Follow logs during commissioning:

```bash
sudo journalctl -u lab-monitor -f
```

A deployment with zero configured Govees is valid.

---

# 12. Configure the Slack app

LabMonitor uses its own Slack app because Socket Mode binds one app-level token
to one process.

Create an app named **LabMonitor** and configure:

1. **Socket Mode**: enable it and create an App-Level Token with
   `connections:write`. Save the `xapp-...` value.
2. **Slash Commands**: create `/labstatus` with description
   `Show current lab temperatures and GPU status`. Socket Mode needs no Request
   URL.
3. **OAuth & Permissions**: add bot scopes `commands` and `chat:write`.
4. **Install to Workspace** and save the bot token (`xoxb-...`).
5. Invite `@LabMonitor` to the alert channel and record that channel's ID.

Add to `/etc/bvl-automations/.lab_monitor.conf`:

```ini
LAB_MONITOR_SLACK_BOT_TOKEN=xoxb-REAL_TOKEN
LAB_MONITOR_SLACK_APP_TOKEN=xapp-REAL_TOKEN
LAB_MONITOR_SLACK_CHANNEL_ID=C_REAL_CHANNEL_ID
```

Restart and verify:

```bash
sudo systemctl restart lab-monitor
sudo journalctl -u lab-monitor -n 50 --no-pager
```

Then run in Slack:

```text
/labstatus
```

The command response is ephemeral. Threshold/recovery transitions are posted
publicly to the configured alert channel.

During commissioning, temporarily lowering a harmless threshold is a useful
way to verify exactly one alert and one recovery. Restore the real threshold
immediately afterward.

---

# 13. Add Govee H5075 sensors

Install the BLE extras if an older deployment was initially installed without
them:

```bash
sudo /opt/bvl-automations/lab-monitor/bin/pip install \
  '/etc/bvl-automations/lab_monitor[slack,ble]'
```

Check the Bluetooth adapter:

```bash
bluetoothctl show
getent group bluetooth
```

Discover sensors:

```bash
sudo /opt/bvl-automations/lab-monitor/bin/python \
  -m lab_monitor discover-govee
```

The command prints each address/readout and a ready-made `[[sensors]]` block.
Add one sensor at a time to `lab_monitor.toml`, assign its room, and restart:

```bash
sudo systemctl restart lab-monitor
sudo journalctl -u lab-monitor -n 100 --no-pager
```

Then verify `/labstatus` and Netdata room history.

Important state semantics:

- never seen: `-- not yet seen`, never alerts;
- awaiting after restart: `-- awaiting reading`, grace period, no alert;
- live: current temperature/humidity;
- stale established sensor: `-- unavailable (!)`, one transition alert.

If discovery works as root but the service cannot scan, inspect BlueZ/D-Bus
permissions for the `labmonitor` service. Do not paper over the problem with
`CAP_NET_RAW`.

---

# 14. Final commissioning checklist

A deployment is considered commissioned only when all of the following pass:

```text
[ ] nvidia-smi works on every GPU machine
[ ] Netdata runs on Parent and every Child
[ ] nvidia_smi contexts exist locally on every GPU machine
[ ] Parent listens on 127.0.0.1:19999 and its intended lab IP
[ ] UFW allows only intended Children/admin networks to 19999
[ ] every Child appears in Parent /api/v3/nodes
[ ] LabMonitor check-config succeeds as the labmonitor user
[ ] LabMonitor status shows plausible current GPU values
[ ] VRAM total was compared against nvidia-smi per GPU model
[ ] lab-monitor.service is active and survives a restart
[ ] /labstatus returns the dashboard in Slack
[ ] one forced test alert and recovery were observed
[ ] Govees either intentionally absent or readable by the service
```

Do not substitute “the service is running” for these checks. Most deployment
mistakes discovered during commissioning were configuration/visibility issues,
not process crashes.

---

# 15. Routine LabMonitor upgrade

The Python environment is separate from the Git checkout, so pulling new code
does not itself update the installed package.

For an ordinary LabMonitor-only upgrade:

```bash
cd /etc/bvl-automations
sudo git pull

sudo /opt/bvl-automations/lab-monitor/bin/pip install \
  '/etc/bvl-automations/lab_monitor[slack,ble]'

sudo systemctl restart lab-monitor
systemctl status lab-monitor --no-pager
sudo journalctl -u lab-monitor -n 50 --no-pager
```

If the systemd unit changed:

```bash
sudo cp /etc/bvl-automations/lab_monitor/systemd/lab-monitor.service \
  /etc/systemd/system/lab-monitor.service
sudo systemctl daemon-reload
sudo systemctl restart lab-monitor
```

If Netdata example configs changed, review the diff and apply the relevant
change manually. Do not blindly overwrite live `netdata.conf` or `stream.conf`.

---

# 16. Replacing or migrating the Parent

The repository is not the complete deployment. Preserve these machine-local
files before replacing the Parent:

```text
/etc/bvl-automations/lab_monitor.toml
/etc/bvl-automations/.lab_monitor.conf
/etc/netdata/netdata.conf
/etc/netdata/stream.conf
/etc/netdata/go.d.conf
/etc/netdata/go.d/nvidia_smi.conf
/etc/netdata/statsd.d/labmonitor.conf
/var/lib/bvl-automations/lab_monitor_state.json   # useful, not essential
```

`stream.conf` contains the streaming UUID. `.lab_monitor.conf` contains Slack
credentials. Treat backups containing either as secrets.

A simple root-only configuration backup is:

```bash
sudo tar -czf /root/labmonitor-config-$(date +%F).tar.gz \
  /etc/bvl-automations/lab_monitor.toml \
  /etc/bvl-automations/.lab_monitor.conf \
  /etc/netdata/netdata.conf \
  /etc/netdata/stream.conf \
  /etc/netdata/go.d.conf \
  /etc/netdata/go.d/nvidia_smi.conf \
  /etc/netdata/statsd.d/labmonitor.conf \
  /var/lib/bvl-automations/lab_monitor_state.json
```

If the state file does not exist yet, omit it from the command. Store the
archive somewhere appropriately protected; it contains secrets.

To replace the Parent:

1. install the repository at `/etc/bvl-automations`;
2. run `sudo ./lab_monitor/install.sh`;
3. install/configure Netdata and NVIDIA collection;
4. restore the live LabMonitor/Netdata config files;
5. update `bind to`, firewall rules, and Child `destination` values if the
   Parent IP changed;
6. verify every Child in `/api/v3/nodes`;
7. run `check-config` and `status` before starting LabMonitor;
8. start the service and verify `/labstatus` plus one test transition.

Historical Netdata telemetry is useful but not required to restore monitoring.
The LabMonitor state file preserves committed alert/sensor-establishment state;
losing it may cause the replacement deployment to relearn state, but does not
change the topology or thresholds.

---

# 17. Known-good commissioned shape

The first exercised Parent deployment used:

```text
Parent role:       DeepThought
Parent OS:         Ubuntu 20.04 / Focal
Python:            managed Python 3.11 via uv
Netdata API:       local Parent on TCP 19999
Dashboard access:  lab interface + UFW allowlist (not open to the internet)
Slack transport:   Socket Mode
Govee transport:   BLE via BlueZ/D-Bus
```

These are a record of what worked, not permanent requirements. Prefer the
validation checks in this runbook over copying old machine-specific values.
