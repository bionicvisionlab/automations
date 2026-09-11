# Bionic Vision Lab Automations

Automations used around the lab.

## ZotBot

-  Reads the ***NEW*** collection on Zotero via Atom feed
-  Determines which items are new
-  Posts new items on the #papers channel on Slack via webhook

Runs every 10 minutes using GitHub Actions.

### Optional lab context

Set two Actions secrets and each announcement gains one sentence on why the paper
matters here, plus up to two @-mentions:

```text
*Lab context:* Phosphene measurements that contradict our axon-map assumptions. @Someone

*Abstract:*
...
```

* `OPENAI_API_KEY`
* `ZOTBOT_LAB_MEMBERS` — the roster below, as raw JSON

The roster stays in the secret; no names, interests or Slack IDs in this public
repo:

```json
[
  {"name": "Example Person", "slack_id": "U0123456789", "research": "Current interests and projects, a sentence or two."},
  {"name": "Another Example", "slack_id": "U9876543210", "research": "Another concise description."}
]
```

Slack IDs: *View full profile → ⋮ → Copy member ID*. Mentions are built from
`slack_id`, never from a name the model returns.

Title, full abstract, Zotero tags and the roster go to `gpt-5.6` — nothing else,
no tools, no web search. Papers without an abstract are not sent. Anything that
goes wrong (no key, bad roster, API error) falls back to the plain announcement;
a paper is never dropped over it.

### Tests

```bash
python -m unittest test_zotbot -v
```

## DiskSentinel

DiskSentinel monitors disk usage and alerts Slack with a per-user /home or /hdd breakdown.

### 1. Create & Configure a Slack App

1. Go to https://api.slack.com/apps and click **Create New App** → **From scratch**  

2. Name it **DiskSentinel**, select your workspace  

3. Under **OAuth & Permissions** → **Scopes**, add Bot Token Scopes:  
   - `chat:write`  
   - `channels:read`  
   - (optional) `chat:write.public` if you want to post in channels without inviting the bot  

4. Install the app to your workspace and authorize  

5. Copy the **Bot User OAuth Token** (`xoxb-…`) and the **Channel ID** (e.g. `C01234567`)

### 2. Clone the repo

As `sudo`, clone into `/etc`:

```bash
cd /etc
sudo git clone https://github.com/bionicvisionlab/automations bvl-automations
```

Make sure the bash script is executable (it should already be):

```bash
cd bvl-automations
chmod +x disk_sentinel.sh
```

### 3. Configure the sentinel

In `/etc/bvl-automations`, create a file called `.disk_sentinel.conf` and export
the following variables:

```bash
SLACK_BOT_TOKEN="xoxb-…"     # from Slack Apps
SLACK_CHANNEL_ID="C01234567" # from channel info
THRESHOLD=90                 # percentage
MOUNT_POINTS="/home /hdd"    # optional
RECOVERY_OFFSET=5            # optional, drop 5% to restore normality
RENOTIFICATION_MINUTES=240   # optional, nag every 240 mins
```

Then lock it down:

```bash
chmod 600 .disk_sentinel.conf
```

### 4. Add to root's crontab

File lock: This wrapper checks if the script is already running. If it is, the new cron job simply quits immediately.
`disk_sentinel.log`: Write output to a log file so the job doesn't fail silently.

```bash
sudo crontab -e
# add a line to run it every 10 mins:
*/10 * * * * /usr/bin/flock -n /var/lock/disk_sentinel.lock /etc/bvl-automations/disk_sentinel.sh >> /var/log/disk_sentinel.log 2>&1
```

## LabMonitor

LabMonitor watches lab temperature and GPU health across the three BioE 3201
workstations. Netdata handles telemetry and history; LabMonitor adds the
physical topology, Govee BLE room sensors, a compact `/labstatus` dashboard in
Slack, and one notification per genuine state change.

Runs as a systemd service on whichever workstation acts as the Netdata Parent.

See [lab_monitor/README.md](lab_monitor/README.md) for deployment and Slack app
setup.

## DeadlineWatcher

DeadlineWatcher posts Slack reminders 3 months, 1 month, 2 weeks, and 1 week before important lab deadlines. Deadlines are scoped to the channel where they are added and are removed automatically after they pass.

### 1. Create & Configure a Slack App

1. Go to https://api.slack.com/apps and click **Create New App** → **From scratch**

2. Name it **DeadlineWatcher** and select your workspace

3. Enable **Socket Mode** and create an App-Level Token with the `connections:write` scope

4. Under **Slash Commands**, create `/deadline`

5. Under **OAuth & Permissions** → **Scopes**, add:

   * `commands`
   * `chat:write`

6. Install the app and copy:

   * Bot User OAuth Token (`xoxb-…`)
   * App-Level Token (`xapp-…`)

7. Invite DeadlineWatcher to each channel where it should operate:

   ```text
   /invite @DeadlineWatcher
   ```

### 2. Configure

Create `/etc/bvl-automations/.deadline_watcher.conf`:

```bash
SLACK_BOT_TOKEN="xoxb-…"
SLACK_APP_TOKEN="xapp-…"
DEADLINE_FILE="/etc/bvl-automations/deadlines.json"  # optional
```

Then:

```bash
chmod 600 /etc/bvl-automations/.deadline_watcher.conf
```

### 3. Commands

```text
/deadline add <YYYY-MM-DD> <title>
/deadline list
/deadline edit <id> <title>
/deadline edit <id> <YYYY-MM-DD> <title>
/deadline remove <id>
/deadline help
```

Examples:

```text
/deadline add 2027-05-23 @Hannah @Lily VSS paper deadline
/deadline add 2027-03-19 @Apurv ISMAR paper deadline (AoE)
```

`/deadline list` shows upcoming deadlines for the current channel, sorted by date:

```text
Mar 19, 2027  [k7m2]  @Apurv ISMAR paper deadline (AoE)
May 23, 2027  [p4x9]  @Hannah @Lily VSS paper deadline
```

Use the generated handle to edit or remove a deadline:

```text
/deadline edit p4x9 @Lily VSS paper deadline
/deadline remove p4x9
```

### 4. Deploy on Ubuntu

Update the repo and dependencies:

```bash
cd /etc/bvl-automations
sudo git pull
sudo npm install
```

Confirm the Node path:

```bash
which node
```

The examples below assume `/usr/bin/node`.

#### Run the Slack app with systemd

Create `/etc/systemd/system/deadline-watcher.service`:

```ini
[Unit]
Description=BVL DeadlineWatcher Slack app
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/etc/bvl-automations
ExecStart=/usr/bin/node /etc/bvl-automations/deadline_watcher.js
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now deadline-watcher
```

Useful commands:

```bash
sudo systemctl status deadline-watcher
sudo systemctl restart deadline-watcher
sudo journalctl -u deadline-watcher -f
```

#### Run the daily reminder check with cron

Add the reminder/pruning pass to root's crontab:

```bash
sudo crontab -e

# run every morning at 9am
0 9 * * * /usr/bin/flock -n /var/lock/deadline_watcher.lock /usr/bin/node /etc/bvl-automations/deadline_watcher.js check >> /var/log/deadline_watcher.log 2>&1
```

If `which node` returns a different path, use that path in both the systemd service and cron entry.

### 5. Tests

```bash
npm test
```
