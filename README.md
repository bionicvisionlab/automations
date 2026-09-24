# Bionic Vision Lab Automations

Automations used around the lab.

## ZotBot

-  Reads the ***NEW*** collection on Zotero via Atom feed
-  Determines which items are new
-  Posts new items on the #papers channel on Slack via `chat.postMessage`

Runs every 10 minutes using GitHub Actions. Needs these Actions secrets:

| Secret | |
| --- | --- |
| `ZOTERO_GROUP`, `ZOTERO_COLLECTION`, `ZOTERO_API_KEY` | Zotero group, the NEW collection key, and an API key |
| `SLACK_BOT_TOKEN` | ZotBot's Slack bot token (`xoxb-...`) |
| `SLACK_CHANNEL_ID` | Channel ID of #papers (`C...`), not its name |

Each post carries invisible Slack message metadata (`bvl.zotbot_paper`, with the
Zotero item key), which is how the journal-club job below finds the paper
behind a message.

### Slack app

One Slack app, shared by ZotBot and the journal-club job:

1. Bot token scopes: `chat:write`, `channels:history`, `reactions:read`.
   No `chat:write.public`.
2. Under *Basic Information → Display Information*, set the name to **ZotBot**
   and the icon to the old `:robot_face:` look. Posts no longer override name
   or icon per message, so this is what everyone sees.
3. Install to the workspace, then `/invite @ZotBot` in #papers.
4. Store the bot token as `SLACK_BOT_TOKEN` and #papers' channel ID
   (*channel details → About*, at the bottom) as `SLACK_CHANNEL_ID`.

Once ZotBot has posted through the bot token, the old `SLACK_WEBHOOK_URL`
secret can be deleted.

### Journal-club nominations

React to any ZotBot post in #papers with `:chefs_kiss:` to nominate that paper
for journal club. At **3** `:chefs_kiss:` reactions, the next daily run adds the
same Zotero item to the Journal Club collection and replies in the thread:

```text
:chefs_kiss: Added to Journal Club.
```

* When the lab-context step thinks a paper plausibly warrants discussion by the
  whole lab, the post also says `*Nominate for journal club?* :chefs_kiss:`.
  That line is only a suggestion; every ZotBot post can be nominated.
* The paper stays in **NEW** and every other collection; Journal Club is added
  alongside. It is the same Zotero item, not a copy.
* Only ZotBot posts from the last 90 days count, and only ones posted since the
  switch to `chat.postMessage` (older posts carry no item key).
* A paper already in Journal Club is left alone, so nothing is posted twice.

`journal_club.py` runs daily via `.github/workflows/journal-club.yml` (or *Run
workflow* by hand). Beyond the Slack secrets above it needs:

| Secret | |
| --- | --- |
| `ZOTERO_JOURNAL_CLUB_COLLECTION` | Key of the existing Journal Club collection |

It reuses `ZOTERO_GROUP` and `ZOTERO_API_KEY`, and the key now needs **write
access** to the group library (zotero.org → *Settings → Security → Keys*).

Check what it would do without writing anything:

```bash
export SLACK_BOT_TOKEN=... SLACK_CHANNEL_ID=... ZOTERO_GROUP=... \
       ZOTERO_API_KEY=... ZOTERO_JOURNAL_CLUB_COLLECTION=...
python journal_club.py --dry-run
```

### Optional lab context

Two Actions secrets add one `*Lab context:*` sentence to each announcement, plus
up to two @-mentions:

* `OPENAI_API_KEY`
* `ZOTBOT_LAB_MEMBERS` — the roster below, as raw JSON

ZotBot matches the paper's Zotero authors against the roster, which picks one of
three tones.

**Someone else's paper** — why it deserves our attention, routed by interest:

```text
*Lab context:* Phosphene measurements that contradict our axon-map assumptions. <@U0123456789>
```

**Ours**, with a student or postdoc author — what the paper shows, first author
first, and mentions only for the paper's own authors:

```text
*Lab context:* Example Person and colleagues show that ..., providing .... Congrats! <@U0123456789>
```

**A collaboration where the PI is our only author** — no name, no congratulations,
no mention:

```text
*Lab context:* In this collaboration, we show that ..., providing ....
```

#### The roster

Names, interests and Slack IDs live in the secret, never in this public repo:

```json
[
  {"name": "Example Person", "slack_id": "U0123456789", "research": "Current interests and projects, a sentence or two."},
  {"name": "Another Example", "slack_id": "U9876543210", "research": "Another concise description."},
  {"name": "Example Chief", "slack_id": "", "research": "Another concise description.", "role": "pi", "notify": false}
]
```

| Field | |
| --- | --- |
| `name` | Required. Matched against the paper's authors. |
| `research` | Required. Drives interest-based routing for outside papers. |
| `slack_id` | Required unless `notify` is `false`. *View full profile → ⋮ → Copy member ID* |
| `role` | `"member"` (default) or `"pi"`. |
| `notify` | `true` (default). `false` takes part in matching but is never @-mentioned. |

`name` has to match the paper's author name exactly; case and extra whitespace
are ignored, nothing else. A middle initial in Zotero reads as a different
person. Only Zotero creators of type `author` count, so editing a volume does
not make it ours. A roster that breaks the rules above disables enrichment and
says so in the Actions log.

Mentions are built from `slack_id`, never from a name the model returns, and are
capped at two.

Title, full abstract, author list, Zotero tags and the roster go to `gpt-5.6`,
one request per paper: no tools, no web search, no stored responses. Papers
without an abstract are skipped. Any failure — missing key, bad roster, API
error — falls back to the plain announcement rather than dropping the paper.

### Tests

```bash
python -m unittest test_zotbot test_journal_club -v
```

## RA Applicant Briefing

Summarizes a new undergraduate RA application into one short Slack briefing:
what the written answers demonstrate, and where the self-reported skill grid
runs ahead of the evidence.

It is a reading aid. It does not score or rank applicants, recommend hiring or
interviewing anyone, or match applicants to lab projects.

Runs inside Google Apps Script, bound to the Form's response Sheet. No server,
no external host.

```text
Google Form
  -> installable on-form-submit trigger
  -> normalizeApplication(e.namedValues)
  -> OpenAI Responses API   (UrlFetchApp.fetch)
  -> formatSlack()
  -> Slack incoming webhook (UrlFetchApp.fetch)
```

### The briefing

```text
*New RA application — Jane Doe · 3rd-year PBS*

*Takeaway:* Concrete psychophysics and participant-running experience; programming evidence is thinner than the grid suggests.

*Demonstrated skills*
🟢 *Human subjects research* — Independently scheduled and ran about 40 participants over two quarters.
🟡 *Eye tracking* — Synchronized an EyeLink 1000; unclear whether they configured it from scratch.
🟠 *EEG/BCI* — Attended BCI club meetings; no recording or analysis described.
⚪ *ML/AI models* — Selected in the grid but absent from the written answers.

*Stands out*
• Diagnosed a 12 ms display-to-tracker lag with a photodiode.

*Gaps / things to clarify*
• Graduates in June, so the time available is about two quarters.
• Did they write the synchronization code or use an existing script?

*Dependability*
🟢 Held the same 8am slot for two quarters and wrote a handoff document before leaving.
```

The marker is how far the *written answers* back the claim:

| | |
| --- | --- |
| 🟢 `substantial` | Describes doing it themselves, with tools, decisions, difficulties or scale. |
| 🟡 `some` | Real hands-on contact, but partial, assisted, or vague about their own part. |
| 🟠 `exposure` | Coursework, a club, a workshop, a tutorial, or watching others. |
| ⚪ `unsupported` | Ticked in the grid, absent from the written answers. |

Only skills worth a sentence appear. Dependability uses the same markers over
`substantial / some / limited / none`. The model returns structured JSON;
`formatSlack()` builds every character of the Slack markup.

### Expected form data

`normalizeApplication()` takes `e.namedValues`, which Apps Script gives as
`{"Question title": ["answer"]}`:

```json
{
  "Timestamp": ["2026-09-19 10:04:11"],
  "Full name": ["Jane Doe"],
  "Class year": ["3rd-year"],
  "Which of the following do you have experience with? [Eye tracking]": ["Yes"],
  "Describe one project or responsibility ...": ["In the Example Perception Lab I ..."]
}
```

* A grid question arrives as one column per row, labelled `Question [Row]`, and
  becomes `selfReportedSkills` keyed by the row label. Answers meaning "not
  selected" (blank, `No`, `None`, ...) are dropped.
* `Timestamp` is dropped and never sent to the model.
* `Full name`, `Class year` and `Major` are lifted out for the Slack header.
  Matching is on the **whole** label against a short alias list, so *"What year
  did you start?"* stays an ordinary response. Set the real labels in
  `FORM_LABELS` at the top of `ra_applicant.js` to bypass the aliases.
* Every other question keeps its text verbatim under `responses`, so rewording a
  form question needs no change here.

Check what a row normalizes to without calling anything:

```bash
node ra_applicant.js ra_application.example.json
```

### Setting up the trigger

1. In the responses Sheet: **Extensions → Apps Script**.
2. Paste `ra_applicant.js` into the project as a single file. The
   `module.exports` block at the bottom is inert in Apps Script.
3. **Project Settings → Script Properties**, add:

   | Property | |
   | --- | --- |
   | `OPENAI_API_KEY` | OpenAI key |
   | `SLACK_WEBHOOK_URL` | Slack incoming webhook for the channel |

   Neither belongs in the script body or in this repo.
4. **Triggers → Add Trigger**: function `handleFormSubmit`, event source
   *From spreadsheet*, event type *On form submit*. It has to be an installable
   trigger; a simple `onFormSubmit` cannot call external services.
5. Submit a test response through the Form and check **Executions**.

Each message ends with an *Open application* link to the submitted row.

### Privacy

Applications are personal data. Requests set `store: false`, failures log an
error name only, and nothing is written anywhere. The applicant's name never
reaches OpenAI; it is used locally for the Slack header. Class year and major
are sent as factual context.

A missing key, HTTP error, refusal or unparseable output all yield a null
analysis, and the Slack message degrades to the header line instead of dropping
the application.

### Tests

```bash
npm test
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
