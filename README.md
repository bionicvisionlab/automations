# Bionic Vision Lab Automations

Automations used around the lab.

## ZotBot

-  Reads the ***NEW*** collection on Zotero via Atom feed
-  Determines which items are new
-  Posts new items on the #papers channel on Slack via webhook

Runs every 5 minutes using GitHub Actions.

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
