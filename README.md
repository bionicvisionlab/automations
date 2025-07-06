# Bionic Vision Lab Automations

Automations used around the lab.

## ZotBot

-  Reads the ***NEW*** collection on Zotero via Atom feed
-  Determines which items are new
-  Posts new items on the #papers channel on Slack via webhook

Runs every 5 minutes using GitHub Actions.

## DiskSentinel

DiskSentinel monitors disk usage and alerts Slack with a per-user /home breakdown.

### 1. Create & Configure a Slack App

1. Go to https://api.slack.com/apps and click **Create New App** → **From scratch**  

2. Name it **DiskSentinel**, select your workspace  

3. Under **OAuth & Permissions** → **Scopes**, add Bot Token Scopes:  
   - `chat:write`  
   - `channels:read`  
   - (optional) `chat:write.public` if you want to post in channels without inviting the bot  

4. Install the app to your workspace and authorize  

5. Copy the **Bot User OAuth Token** (`xoxb-…`) and the **Channel ID** (e.g. `C01234567`)

### 2. Create your config file
Create `~/.disk_sentinel.conf` with:
```bash
SLACK_BOT_TOKEN="xoxb-…"
SLACK_CHANNEL_ID="C01234567"
THRESHOLD=85
```

Then lock it down:

```bash
chmod 600 ~/.disk_sentinel.conf
```

### 3. Set up bash script

Make sure the bash script is executable:

```bash
chmod +x ~/source/bvl-automations/disk_sentinel.sh
```

and has password-less `sudo` privileges for `du` and `df`:

```bash
sudo visudo
# add line at the end:
$USER ALL=(root) NOPASSWD: /usr/bin/du, /usr/bin/df
```

where `$USER` is obviously your username. Alternatively, you could put this
somewhere where `crontab` runs with `root` access, e.g. `/etc` or
`/usr/local/bin`.

### 4. Run your user's crontab editor:

```bash
crontab -e
```

Add this line to run every 5 minutes:

```bash
*/5 * * * * ~/source/bvl-automations/disk_sentinel.sh
```