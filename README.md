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

## DeadlineWatcher

A small Slack reminder utility for important future lab deadlines — conference
and grant submissions. It is deliberately **not** a task manager: there is no
completion state, no snoozing, no recurrence and no assignee model. Its only job
is to ping the right channel far enough ahead that people can still act.

**Deadlines belong to the Slack channel where they are created.** A deadline
added in a private channel stays private to that channel; one added in a project
channel reminds that project; one added in `#general` is lab-wide. Reminders are
always posted back to the channel the deadline was created in.

Each deadline is reminded about **3 months, 1 month, 2 weeks and 1 week** before
its date — a fixed, non-configurable cadence using calendar arithmetic. There are
no last-minute reminders. Once the date has passed, the deadline is deleted
automatically; there is no archive.

### 1. Create & Configure a Slack App

1. Go to https://api.slack.com/apps and click **Create New App** → **From scratch**

2. Name it **DeadlineWatcher**, select your workspace

3. Under **Socket Mode**, enable Socket Mode. This creates an **App-Level Token**
   with the `connections:write` scope — copy it (`xapp-…`)

4. Under **Slash Commands**, create the command `/deadline` (any description;
   the Request URL is unused in Socket Mode). Suggested usage hint:
   `add <YYYY-MM-DD> <title> | list | edit <id> … | remove <id> | help`

5. Under **OAuth & Permissions** → **Scopes**, add Bot Token Scopes:
   - `commands` (added automatically with the slash command)
   - `chat:write` — needed to post the scheduled reminders
   - (optional) `chat:write.public` if you want reminders in public channels the
     bot has not been invited to

6. Install the app to your workspace and copy the **Bot User OAuth Token**
   (`xoxb-…`)

7. **Invite the bot into every channel it should operate in**
   (`/invite @DeadlineWatcher`). This is mandatory for private channels —
   `chat:write.public` does not cover them — otherwise the daily reminder post
   fails with `not_in_channel`.

### 2. Configure

The script reads `/etc/bvl-automations/.deadline_watcher.conf` (same style as
DiskSentinel), so cron does not need any environment set up:

```bash
SLACK_BOT_TOKEN="xoxb-…"   # Bot User OAuth Token
SLACK_APP_TOKEN="xapp-…"   # App-Level Token, Socket Mode
DEADLINE_FILE="/etc/bvl-automations/deadlines.json"  # optional, this is the default
```

Then lock it down:

```bash
chmod 600 /etc/bvl-automations/.deadline_watcher.conf
```

`DEADLINE_FILE` is the JSON file holding the deadlines. It only needs to be set
for tests or alternate deployments; the default is
`/etc/bvl-automations/deadlines.json`, which is created on first write.

### 3. Commands

All management commands reply **ephemerally** (only the person who typed them
sees the answer), so managing deadlines never spams the channel. Only the
scheduled reminders are posted publicly.

```text
/deadline add <YYYY-MM-DD> <title>
/deadline list
/deadline edit <id> <title>
/deadline edit <id> <YYYY-MM-DD> <title>
/deadline remove <id>          (rm and delete also work)
/deadline help
```

The title is free text and may contain any number of Slack mentions, which are
preserved verbatim so the reminder pings those people:

```text
/deadline add 2027-05-23 @Hannah @Lily VSS paper deadline
/deadline add 2027-03-19 @Apurv @Lucas ISMAR paper deadline
```

DeadlineWatcher works on plain calendar dates, not timestamps, so there is no
timezone handling. If a conference deadline is in AoE, just say so in the title:

```text
/deadline add 2027-03-19 ISMAR paper deadline (AoE)
```

Adding a deadline assigns it a short random handle. `/deadline list` shows the
handles, sorted by date, for the current channel only:

```text
Upcoming deadlines:

Mar 19, 2027  [k7m2]  ISMAR paper deadline
May 23, 2027  [p4x9]  @Hannah @Lily VSS paper deadline
Oct 15, 2027  [q2fd]  CHI paper deadline
```

Use the handle to edit or remove. Editing the title is also how you drop someone
from a deadline once they are no longer involved:

```text
/deadline edit p4x9 @Lily VSS paper deadline
/deadline edit p4x9 2027-05-24 @Lily VSS paper deadline
/deadline remove q2fd
```

You can only edit or remove deadlines belonging to the channel you are typing in.

A reminder looks like this:

```text
📅 3 months until @Hannah @Lily VSS paper deadline
Deadline: May 23, 2027
```

### 4. Deploy on the Ubuntu machine

Requires Node.js 18 or newer.

```bash
cd /etc/bvl-automations
sudo git pull
sudo npm install
```

Create `.deadline_watcher.conf` as described above, then run the Socket Mode app,
which handles the `/deadline` slash command:

```bash
node /etc/bvl-automations/deadline_watcher.js
```

> **Note:** this is a long-running process and must be kept alive by something.
> This repository does not currently ship or document a supervision mechanism
> (systemd unit, tmux session, …) for it — set one up manually to taste. Without
> it, the slash command stops responding whenever the process exits, though
> already-stored deadlines are unaffected and the daily reminders below keep
> working, since they run independently.

The reminders themselves come from a separate once-a-day pass that prunes expired
deadlines, posts anything due today, and exits:

```bash
node /etc/bvl-automations/deadline_watcher.js check
```

Add that to root's crontab to run each morning in the machine's local timezone.
Confirm the Node path first with `which node` (often `/usr/bin/node`, but
`/usr/local/bin/node` under nvm or a NodeSource install) and use it below:

```bash
sudo crontab -e
# add a line to run it every morning at 9am:
0 9 * * * /usr/bin/flock -n /var/lock/deadline_watcher.lock /usr/bin/node /etc/bvl-automations/deadline_watcher.js check >> /var/log/deadline_watcher.log 2>&1
```

Reminder delivery is intentionally stateless: a reminder is sent only when
today's date is exactly a milestone date. If the machine is down that day, that
one reminder is missed. That is an accepted trade-off for keeping the tool simple.

### 5. Tests

```bash
npm test
```
