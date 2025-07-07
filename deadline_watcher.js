// /etc/bvl-automations/deadline_watcher.js
// Socket Mode Slack app for managing conference/grant deadlines with timezone support

require('dotenv').config({ path: '/etc/bvl-automations/.deadline_watcher.conf' });
const { App, SocketModeReceiver } = require('@slack/bolt');
const { DateTime } = require('luxon');
const fs = require('fs');

// Map common zone codes to IANA names
const ZONE_ALIASES = {
  UTC: 'UTC',
  PST: 'America/Los_Angeles',
  AoE: 'Etc/GMT+12'
};

// Path to the JSON file storing deadlines
const DEADLINE_FILE = '/etc/bvl-automations/deadlines.json';

// Initialize Bolt app in Socket Mode
const receiver = new SocketModeReceiver({ appToken: process.env.SLACK_APP_TOKEN });
const app = new App({ token: process.env.SLACK_BOT_TOKEN, receiver });

// Helper: format YYYY-MM-DD to "DD MMM YYYY"
function formatDate(isoDate, tz) {
  return DateTime.fromISO(isoDate, { zone: ZONE_ALIASES[tz] })
    .toFormat('dd LLL yyyy');
}

// Load, prune, sort, and save deadlines
function loadDeadlines() {
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(DEADLINE_FILE, 'utf8'));
  } catch {
    raw = [];
  }
  const today = DateTime.now().startOf('day');
  const upcoming = raw
    .map(d => ({ id: d.id, title: d.title, date: d.date, tz: d.tz || 'UTC' }))
    .filter(d => DateTime.fromISO(d.date, { zone: ZONE_ALIASES[d.tz] }).startOf('day') >= today)
    .sort((a, b) => DateTime.fromISO(a.date, { zone: ZONE_ALIASES[a.tz] }) - DateTime.fromISO(b.date, { zone: ZONE_ALIASES[b.tz] }));
  // If pruning happened, write back full upcoming array
  if (upcoming.length !== raw.length) {
    fs.writeFileSync(DEADLINE_FILE, JSON.stringify(upcoming, null, 2));
  }
  return upcoming;
}

// Parse slash-command arguments: subcommand, id, date, optional tz, then title
function parseArgs(text) {
  const parts = text.trim().split(/\s+/);
  const sub  = (parts[0] || 'help').toLowerCase();
  const id   = parts[1] || '';
  const date = parts[2] || '';
  let tz     = 'UTC';
  let idx    = 3;
  if (parts[3] && ZONE_ALIASES[parts[3]]) {
    tz = parts[3];
    idx = 4;
  }
  const title = parts.slice(idx).join(' ');
  return { sub, id, date, tz, title };
}

// Slash command handler for /deadline
app.command('/deadline', async ({ command, ack, say, respond }) => {
  await ack(); // always acknowledge first
  const { sub, id, date, tz, title } = parseArgs(command.text);
  // Work on the upcoming list
  let list = loadDeadlines();
  let msg;
  let responseType = 'ephemeral';

  switch (sub) {
    case 'add':
      if (!id || !date || !title) {
        msg = ':warning: Usage: `/deadline add <id> <YYYY-MM-DD> [TZ] <Title>`';
      } else if (!DateTime.fromISO(date).isValid) {
        msg = ':warning: Invalid date format. Use YYYY-MM-DD.';
      } else if (list.some(d => d.id.toLowerCase() === id.toLowerCase())) {
        msg = `:warning: A deadline with ID *${id}* already exists.`;
      } else {
        list.push({ id, title, date, tz });
        fs.writeFileSync(DEADLINE_FILE, JSON.stringify(list, null, 2));
        msg = `:white_check_mark: Added *${title}* (${id}) on \`${formatDate(date, tz)} ${tz}\``;
      }
      responseType = 'in_channel';
      break;

    case 'remove':
    case 'rm':
    case 'delete':
      if (!id) {
        msg = ':warning: Usage: `/deadline remove <id>`';
      } else if (!list.some(d => d.id.toLowerCase() === id.toLowerCase())) {
        msg = `:warning: No deadline with ID *${id}* found.`;
      } else {
        list = list.filter(d => d.id.toLowerCase() !== id.toLowerCase());
        fs.writeFileSync(DEADLINE_FILE, JSON.stringify(list, null, 2));
        msg = `:wastebasket: Removed deadline *${id}*.`;
      }
      responseType = 'in_channel';
      break;

    case 'clear':
    case 'reset':
      list = [];
      fs.writeFileSync(DEADLINE_FILE, '[]');
      msg = ':wastebasket: All deadlines cleared.';
      responseType = 'in_channel';
      break;

    case 'list':
      if (!list.length) {
        msg = '_No upcoming deadlines._';
      } else {
        const lines = list.map(d => `• *${formatDate(d.date, d.tz)} ${d.tz}* — \`${d.id}\`: ${d.title}`);
        msg = `*Upcoming Deadlines:*
${lines.join('\n')}`;
      }
      break;

    default:
      msg = '*DeadlineWatcher Commands*\n'
        + '• `/deadline add <id> <YYYY-MM-DD> [TZ] <Title>` — Add a deadline\n'
        + '• `/deadline remove <id>` — Remove a deadline\n'
        + '• `/deadline clear` — Clear all deadlines\n'
        + '• `/deadline list` — List upcoming deadlines\n'
        + '• `/deadline help` — Show this message';
  }

  switch (responseType) {
    case 'in_channel':
      await say({ text: msg, response_type: 'in_channel' });
      break;

    case 'ephemeral':
    default:
      await respond({ text: msg, response_type: 'ephemeral' });
  }

  //  await say({ response_type: responseType, text: msg });
});

// Start the app
(async () => {
  await app.start();
  console.log('⚡️ DeadlineWatcher running in Socket Mode');
})();

