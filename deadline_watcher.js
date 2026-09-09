// /etc/bvl-automations/deadline_watcher.js
//
// DeadlineWatcher: a small Slack reminder utility for important lab deadlines
// (conference/grant submissions). Deliberately not a task manager.
//
//   node deadline_watcher.js         start the Socket Mode Slack app
//   node deadline_watcher.js check   run the daily reminder/prune pass and exit
//
// Deadlines belong to the Slack channel they were created in; reminders are
// always posted back to that channel.

const fs = require('fs');
const path = require('path');
const { DateTime } = require('luxon');

const CONFIG_FILE = '/etc/bvl-automations/.deadline_watcher.conf';
const DEFAULT_DEADLINE_FILE = '/etc/bvl-automations/deadlines.json';

// Fixed, global reminder cadence. Calendar arithmetic, not day approximations.
const MILESTONES = [
  { label: '3 months', offset: { months: 3 } },
  { label: '1 month', offset: { months: 1 } },
  { label: '2 weeks', offset: { weeks: 2 } },
  { label: '1 week', offset: { weeks: 1 } }
];

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ID_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789';

// ---------------------------------------------------------------------------
// Dates
// ---------------------------------------------------------------------------

// Today's calendar date in the server's local zone, as YYYY-MM-DD.
function today() {
  return DateTime.now().toISODate();
}

function isValidDate(value) {
  return ISO_DATE.test(value || '') && DateTime.fromISO(value).isValid;
}

// "2027-05-23" -> "May 23, 2027"
function formatDate(isoDate) {
  const dt = DateTime.fromISO(isoDate);
  return dt.isValid ? dt.toFormat('LLL d, yyyy') : String(isoDate);
}

// A deadline stays active through its stated calendar date. ISO dates sort and
// compare correctly as plain strings.
function isExpired(deadline, todayISO) {
  return String(deadline.date) < todayISO;
}

// Returns the milestone label ("1 month", ...) if todayISO is exactly one of
// the deadline's milestone dates, otherwise null.
function getReminderMilestone(isoDate, todayISO) {
  const deadline = DateTime.fromISO(isoDate);
  if (!deadline.isValid) return null;
  for (const { label, offset } of MILESTONES) {
    if (deadline.minus(offset).toISODate() === todayISO) return label;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Deadline records
// ---------------------------------------------------------------------------

function randomId(length) {
  let id = '';
  for (let i = 0; i < length; i++) {
    id += ID_ALPHABET[Math.floor(Math.random() * ID_ALPHABET.length)];
  }
  return id;
}

// Short, stable, user-visible handle. Regenerates on collision with any
// existing deadline (across all channels, so handles read unambiguously).
function generateId(existing = [], randomChars = randomId) {
  const taken = new Set(existing.map(d => String(d.id || '').toLowerCase()));
  for (let attempt = 0; attempt < 50; attempt++) {
    const id = randomChars(attempt < 25 ? 4 : 6);
    if (!taken.has(id)) return id;
  }
  throw new Error('DeadlineWatcher: could not generate a unique deadline id');
}

function sortDeadlines(list) {
  return [...list].sort((a, b) => String(a.date).localeCompare(String(b.date)));
}

function getChannelDeadlines(list, channel) {
  return sortDeadlines(list.filter(d => d.channel === channel));
}

function findInChannel(list, id, channel) {
  const wanted = String(id || '').toLowerCase();
  return list.find(
    d => d.channel === channel && String(d.id || '').toLowerCase() === wanted
  );
}

function pruneExpired(list, todayISO) {
  return list.filter(d => !isExpired(d, todayISO));
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

function deadlineFile() {
  return process.env.DEADLINE_FILE || DEFAULT_DEADLINE_FILE;
}

// Records are returned as stored: never reconstruct from a fixed field list,
// or fields added later are silently dropped.
function loadDeadlines(file = deadlineFile()) {
  try {
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

function saveDeadlines(list, file = deadlineFile()) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(list, null, 2) + '\n');
}

// ---------------------------------------------------------------------------
// Command handling
// ---------------------------------------------------------------------------

const HELP =
  '*DeadlineWatcher* — reminders for this channel\n' +
  '• `/deadline add <YYYY-MM-DD> <title>` — add a deadline (mentions in the title get pinged)\n' +
  '• `/deadline list` — list this channel\'s deadlines\n' +
  '• `/deadline edit <id> <title>` — change the title (includes @ mentions)\n' +
  '• `/deadline edit <id> <YYYY-MM-DD> <title>` — change date and title\n' +
  '• `/deadline remove <id>` — remove a deadline\n' +
  '• `/deadline preview <id>` — preview reminder\n' +
  '• `/deadline help` — show this message\n' +
  '_Reminders are posted here 3 months, 1 month, 2 weeks and 1 week ahead. ' +
  'Deadlines disappear on their own once the date has passed._';

function parseCommand(text) {
  const parts = String(text || '').trim().split(/\s+/).filter(Boolean);
  return { sub: (parts[0] || 'help').toLowerCase(), args: parts.slice(1) };
}

function formatDeadline(d) {
  return `${formatDate(d.date)}  [${d.id}]  ${d.title}`;
}

function warn(message) {
  return `:warning: ${message}`;
}

// Applies a slash command to `list` and returns the resulting list plus the
// (ephemeral) reply. Pure: no I/O, no Slack. `changed` says whether the caller
// needs to persist.
function applyCommand(list, { text, channel, todayISO = today(), generate = generateId } = {}) {
  const { sub, args } = parseCommand(text);
  const unchanged = message => ({ list, text: message, changed: false });

  switch (sub) {
    case 'add': {
      const date = args[0] || '';
      const title = args.slice(1).join(' ').trim();
      if (!date || !title) {
        return unchanged(warn('Usage: `/deadline add <YYYY-MM-DD> <title>`'));
      }
      if (!isValidDate(date)) {
        return unchanged(warn(`Invalid date \`${date}\`. Use YYYY-MM-DD.`));
      }
      if (date < todayISO) {
        return unchanged(warn(`\`${date}\` is already in the past.`));
      }
      const duplicate = list.find(
        d => d.channel === channel && d.date === date && d.title === title
      );
      if (duplicate) {
        return unchanged(
          warn(`That deadline already exists here: ${formatDeadline(duplicate)}`)
        );
      }
      const entry = { id: generate(list), title, date, channel };
      return {
        list: [...list, entry],
        text: `:white_check_mark: Added  ${formatDeadline(entry)}`,
        changed: true
      };
    }

    case 'edit': {
      const id = args[0] || '';
      if (!id || args.length < 2) {
        return unchanged(
          warn('Usage: `/deadline edit <id> [YYYY-MM-DD] <title>`')
        );
      }
      const existing = findInChannel(list, id, channel);
      if (!existing) {
        return unchanged(warn(`No deadline \`${id}\` in this channel.`));
      }
      const hasDate = ISO_DATE.test(args[1]);
      const date = hasDate ? args[1] : existing.date;
      const title = args.slice(hasDate ? 2 : 1).join(' ').trim();
      if (!title) {
        return unchanged(
          warn('Usage: `/deadline edit <id> [YYYY-MM-DD] <title>`')
        );
      }
      if (!isValidDate(date)) {
        return unchanged(warn(`Invalid date \`${args[1]}\`. Use YYYY-MM-DD.`));
      }
      const updated = { ...existing, date, title };
      return {
        list: list.map(d => (d === existing ? updated : d)),
        text: `:pencil2: Updated  ${formatDeadline(updated)}`,
        changed: true
      };
    }

    case 'remove':
    case 'rm':
    case 'delete': {
      const id = args[0] || '';
      if (!id) return unchanged(warn('Usage: `/deadline remove <id>`'));
      const existing = findInChannel(list, id, channel);
      if (!existing) {
        return unchanged(warn(`No deadline \`${id}\` in this channel.`));
      }
      return {
        list: list.filter(d => d !== existing),
        text: `:wastebasket: Removed  ${formatDeadline(existing)}`,
        changed: true
      };
    }

    case 'list': {
      const mine = getChannelDeadlines(list, channel).filter(
        d => !isExpired(d, todayISO)
      );
      if (!mine.length) return unchanged('_No upcoming deadlines in this channel._');
      return unchanged(
        `*Upcoming deadlines:*\n\n${mine.map(formatDeadline).join('\n')}`
      );
    }

    case 'preview': {
      const id = args[0] || '';
      const existing = findInChannel(list, id, channel);
      if (!existing) {
        return unchanged(warn(`No deadline \`${id}\` in this channel.`));
      }
      return unchanged(reminderText(existing, '1 month'));
    }

    default:
      return unchanged(HELP);
  }
}

// ---------------------------------------------------------------------------
// Daily check
// ---------------------------------------------------------------------------

function reminderText(deadline, label) {
  return `📅 ${label} until ${deadline.title}\nDeadline: ${formatDate(deadline.date)}`;
}

// Reminders due today, one per deadline, addressed to the stored channel.
function dueReminders(list, todayISO) {
  const due = [];
  for (const d of list) {
    const label = getReminderMilestone(d.date, todayISO);
    if (label) due.push({ channel: d.channel, text: reminderText(d, label) });
  }
  return due;
}

async function postToSlack(channel, text) {
  const { WebClient } = require('@slack/web-api');
  const client = new WebClient(process.env.SLACK_BOT_TOKEN);
  await client.chat.postMessage({ channel, text, unfurl_links: false });
}

// Prune expired deadlines, post today's reminders, persist if anything changed.
async function runCheck({ todayISO = today(), file = deadlineFile(), post = postToSlack } = {}) {
  const list = loadDeadlines(file);
  const kept = pruneExpired(list, todayISO);
  const reminders = dueReminders(kept, todayISO);

  for (const { channel, text } of reminders) {
    try {
      await post(channel, text);
    } catch (err) {
      console.error(`DeadlineWatcher: failed to post to ${channel}: ${err.message}`);
    }
  }
  if (kept.length !== list.length) saveDeadlines(kept, file);
  return { pruned: list.length - kept.length, reminders };
}

// ---------------------------------------------------------------------------
// Slack Socket Mode app
// ---------------------------------------------------------------------------

async function startApp() {
  const { App, SocketModeReceiver } = require('@slack/bolt');
  const receiver = new SocketModeReceiver({ appToken: process.env.SLACK_APP_TOKEN });
  const app = new App({ token: process.env.SLACK_BOT_TOKEN, receiver });

  app.command('/deadline', async ({ command, ack, respond }) => {
    await ack();
    const result = applyCommand(loadDeadlines(), {
      text: command.text,
      channel: command.channel_id
    });
    if (result.changed) saveDeadlines(result.list);
    // Management commands are always ephemeral; only scheduled reminders are public.
    await respond({ text: result.text, response_type: 'ephemeral' });
  });

  await app.start();
  console.log('⚡️ DeadlineWatcher running in Socket Mode');
}

if (require.main === module) {
  require('dotenv').config({ path: CONFIG_FILE, quiet: true });
  if (process.argv[2] === 'check') {
    runCheck().catch(err => {
      console.error(`DeadlineWatcher check failed: ${err.message}`);
      process.exit(1);
    });
  } else {
    startApp().catch(err => {
      console.error(`DeadlineWatcher failed to start: ${err.message}`);
      process.exit(1);
    });
  }
}

module.exports = {
  MILESTONES,
  applyCommand,
  dueReminders,
  findInChannel,
  formatDate,
  formatDeadline,
  generateId,
  getChannelDeadlines,
  getReminderMilestone,
  isExpired,
  isValidDate,
  loadDeadlines,
  parseCommand,
  pruneExpired,
  reminderText,
  runCheck,
  saveDeadlines,
  sortDeadlines,
  today
};
