const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const dw = require('./deadline_watcher');

const CH_A = 'C_AAA';
const CH_B = 'C_BBB';

function deadline(over = {}) {
  return { id: 'p4x9', title: 'VSS paper deadline', date: '2027-05-23', channel: CH_A, ...over };
}

function tmpFile(name) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dw-test-'));
  return path.join(dir, name);
}

// Deterministic id generator for tests.
function fakeGenerator(...ids) {
  let i = 0;
  return () => ids[Math.min(i++, ids.length - 1)];
}

// ---------------------------------------------------------------------------
// Date / reminder behavior
// ---------------------------------------------------------------------------

test('three-calendar-month milestone', () => {
  assert.equal(dw.getReminderMilestone('2027-05-23', '2027-02-23'), '3 months');
});

test('one-calendar-month milestone', () => {
  assert.equal(dw.getReminderMilestone('2027-05-23', '2027-04-23'), '1 month');
});

test('two-week milestone', () => {
  assert.equal(dw.getReminderMilestone('2027-05-23', '2027-05-09'), '2 weeks');
});

test('one-week milestone', () => {
  assert.equal(dw.getReminderMilestone('2027-05-23', '2027-05-16'), '1 week');
});

test('calendar months of unequal length follow Luxon arithmetic', () => {
  // May 31 minus 3 months clamps to Feb 28 (2027 is not a leap year).
  assert.equal(dw.getReminderMilestone('2027-05-31', '2027-02-28'), '3 months');
  // March 31 minus 1 month clamps to Feb 28, not to a 30-day approximation.
  assert.equal(dw.getReminderMilestone('2027-03-31', '2027-02-28'), '1 month');
  assert.equal(dw.getReminderMilestone('2027-03-31', '2027-03-01'), null);
  // Leap year: May 31 minus 3 months is Feb 29.
  assert.equal(dw.getReminderMilestone('2028-05-31', '2028-02-29'), '3 months');
});

test('no reminder on unrelated days', () => {
  for (const day of ['2027-05-22', '2027-05-23', '2027-05-10', '2027-01-23', '2027-04-22']) {
    assert.equal(dw.getReminderMilestone('2027-05-23', day), null, `unexpected reminder on ${day}`);
  }
});

test('no reminder for an invalid stored date', () => {
  assert.equal(dw.getReminderMilestone('not-a-date', '2027-05-16'), null);
});

test('deadline remains active on its stated date', () => {
  assert.equal(dw.isExpired(deadline(), '2027-05-23'), false);
  assert.equal(dw.isExpired(deadline(), '2027-05-22'), false);
});

test('deadline is expired the day after its stated date', () => {
  assert.equal(dw.isExpired(deadline(), '2027-05-24'), true);
});

// ---------------------------------------------------------------------------
// Channel scoping
// ---------------------------------------------------------------------------

test('list only shows deadlines from the current channel, chronologically', () => {
  const list = [
    deadline({ id: 'q2fd', date: '2027-10-15', title: 'CHI paper deadline' }),
    deadline({ id: 'zzz1', date: '2027-01-05', title: 'Secret channel B deadline', channel: CH_B }),
    deadline({ id: 'k7m2', date: '2027-03-19', title: 'ISMAR paper deadline' })
  ];
  const out = dw.applyCommand(list, { text: 'list', channel: CH_A, todayISO: '2026-09-09' });

  assert.match(out.text, /Mar 19, 2027 {2}\[k7m2\] {2}ISMAR paper deadline/);
  assert.match(out.text, /Oct 15, 2027 {2}\[q2fd\] {2}CHI paper deadline/);
  assert.doesNotMatch(out.text, /Secret channel B/);
  assert.ok(out.text.indexOf('k7m2') < out.text.indexOf('q2fd'), 'sorted by date');
  assert.equal(out.changed, false);
});

test('list hides deadlines whose date has passed', () => {
  const list = [deadline({ date: '2026-01-01' })];
  const out = dw.applyCommand(list, { text: 'list', channel: CH_A, todayISO: '2026-09-09' });
  assert.match(out.text, /No upcoming deadlines/);
});

test('channel B cannot edit channel A deadline', () => {
  const list = [deadline()];
  const out = dw.applyCommand(list, {
    text: 'edit p4x9 hijacked title',
    channel: CH_B,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, false);
  assert.match(out.text, /No deadline `p4x9` in this channel/);
  assert.equal(list[0].title, 'VSS paper deadline');
});

test('channel B cannot remove channel A deadline', () => {
  const list = [deadline()];
  const out = dw.applyCommand(list, { text: 'remove p4x9', channel: CH_B, todayISO: '2026-09-09' });
  assert.equal(out.changed, false);
  assert.equal(out.list.length, 1);
});

test('reminders are addressed to the deadline stored channel', () => {
  const list = [
    deadline({ id: 'aaa1', date: '2027-05-23', channel: CH_A }),
    deadline({ id: 'bbb1', date: '2027-05-23', channel: CH_B, title: 'Grant deadline' })
  ];
  const due = dw.dueReminders(list, '2027-04-23');
  assert.deepEqual(due.map(r => r.channel), [CH_A, CH_B]);
});

// ---------------------------------------------------------------------------
// IDs
// ---------------------------------------------------------------------------

test('add generates a short lowercase alphanumeric id', () => {
  const out = dw.applyCommand([], {
    text: 'add 2027-03-19 ISMAR paper deadline',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, true);
  assert.match(out.list[0].id, /^[a-z0-9]{4,6}$/);
});

test('an id stays stable across edits', () => {
  let list = dw.applyCommand([], {
    text: 'add 2027-03-19 ISMAR paper deadline',
    channel: CH_A,
    todayISO: '2026-09-09',
    generate: fakeGenerator('k7m2')
  }).list;
  list = dw.applyCommand(list, {
    text: 'edit k7m2 2027-03-20 ISMAR paper deadline (AoE)',
    channel: CH_A,
    todayISO: '2026-09-09'
  }).list;
  assert.equal(list[0].id, 'k7m2');
});

test('generateId regenerates on collision instead of reusing a taken id', () => {
  const existing = [deadline({ id: 'aaaa' }), deadline({ id: 'bbbb', channel: CH_B })];
  const id = dw.generateId(existing, fakeGenerator('aaaa', 'bbbb', 'cccc'));
  assert.equal(id, 'cccc');
});

test('generateId collision check is case-insensitive', () => {
  const id = dw.generateId([deadline({ id: 'AAAA' })], fakeGenerator('aaaa', 'dddd'));
  assert.equal(id, 'dddd');
});

test('adding never overwrites an existing deadline on id collision', () => {
  const list = [deadline({ id: 'aaaa' })];
  const out = dw.applyCommand(list, {
    text: 'add 2027-03-19 ISMAR paper deadline',
    channel: CH_A,
    todayISO: '2026-09-09',
    generate: existing => dw.generateId(existing, fakeGenerator('aaaa', 'eeee'))
  });
  assert.equal(out.list.length, 2);
  assert.deepEqual(out.list.map(d => d.id), ['aaaa', 'eeee']);
});

// ---------------------------------------------------------------------------
// Adding
// ---------------------------------------------------------------------------

test('add stores date, title and invoking channel', () => {
  const out = dw.applyCommand([], {
    text: 'add 2027-05-23 <@U123> <@U456> VSS paper deadline',
    channel: CH_A,
    todayISO: '2026-09-09',
    generate: fakeGenerator('p4x9')
  });
  assert.deepEqual(out.list[0], {
    id: 'p4x9',
    title: '<@U123> <@U456> VSS paper deadline',
    date: '2027-05-23',
    channel: CH_A
  });
  assert.match(out.text, /May 23, 2027 {2}\[p4x9\]/);
});

test('channel mentions in the title do not affect routing', () => {
  const out = dw.applyCommand([], {
    text: 'add 2027-05-23 <!channel> <#C999|general> VSS paper deadline',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.list[0].channel, CH_A);
  assert.equal(out.list[0].title, '<!channel> <#C999|general> VSS paper deadline');
});

test('add rejects an invalid date', () => {
  for (const bad of ['2027-13-01', '19-03-2027', 'next-friday', '2027-02-30']) {
    const out = dw.applyCommand([], {
      text: `add ${bad} Some deadline`,
      channel: CH_A,
      todayISO: '2026-09-09'
    });
    assert.equal(out.changed, false, `${bad} should be rejected`);
    assert.match(out.text, /Invalid date|Usage/);
  }
});

test('add rejects a missing title', () => {
  const out = dw.applyCommand([], { text: 'add 2027-05-23', channel: CH_A, todayISO: '2026-09-09' });
  assert.equal(out.changed, false);
  assert.match(out.text, /Usage/);
});

test('add rejects a date in the past', () => {
  const out = dw.applyCommand([], {
    text: 'add 2020-01-01 Long gone',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, false);
  assert.match(out.text, /already in the past/);
});

test('add rejects an exact duplicate in the same channel', () => {
  const list = [deadline()];
  const out = dw.applyCommand(list, {
    text: 'add 2027-05-23 VSS paper deadline',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, false);
  assert.match(out.text, /already exists/);
});

test('the same date and title in a different channel is allowed', () => {
  const list = [deadline()];
  const out = dw.applyCommand(list, {
    text: 'add 2027-05-23 VSS paper deadline',
    channel: CH_B,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, true);
  assert.equal(out.list.length, 2);
});

test('a similar but not identical title is allowed', () => {
  const list = [deadline()];
  const out = dw.applyCommand(list, {
    text: 'add 2027-05-23 VSS poster deadline',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, true);
});

// ---------------------------------------------------------------------------
// Editing
// ---------------------------------------------------------------------------

test('title-only edit preserves the date', () => {
  const list = [deadline({ title: '<@U123> <@U456> VSS paper deadline' })];
  const out = dw.applyCommand(list, {
    text: 'edit p4x9 <@U456> VSS paper deadline',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, true);
  assert.deepEqual(out.list[0], {
    id: 'p4x9',
    title: '<@U456> VSS paper deadline',
    date: '2027-05-23',
    channel: CH_A
  });
  assert.match(out.text, /Updated {2}May 23, 2027 {2}\[p4x9\] {2}<@U456> VSS paper deadline/);
});

test('date + title edit updates both', () => {
  const out = dw.applyCommand([deadline()], {
    text: 'edit p4x9 2027-05-24 <@U456> VSS paper deadline',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.list[0].date, '2027-05-24');
  assert.equal(out.list[0].title, '<@U456> VSS paper deadline');
});

test('edit preserves Slack mention markup verbatim', () => {
  const title = '<@U123> <@U456> <!here> ISMAR paper deadline (AoE)';
  const out = dw.applyCommand([deadline()], {
    text: `edit p4x9 ${title}`,
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.list[0].title, title);
});

test('edit is case-insensitive on the id', () => {
  const out = dw.applyCommand([deadline()], {
    text: 'edit P4X9 New title',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, true);
  assert.equal(out.list[0].title, 'New title');
});

test('edit rejects an unknown id', () => {
  const out = dw.applyCommand([deadline()], {
    text: 'edit nope New title',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, false);
  assert.match(out.text, /No deadline `nope`/);
});

test('edit without a new title reports usage', () => {
  const out = dw.applyCommand([deadline()], {
    text: 'edit p4x9',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, false);
  assert.match(out.text, /Usage/);
});

test('edit preserves fields the command does not touch', () => {
  const list = [deadline({ note: 'kept' })];
  const out = dw.applyCommand(list, {
    text: 'edit p4x9 New title',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.list[0].note, 'kept');
});

// ---------------------------------------------------------------------------
// Removing
// ---------------------------------------------------------------------------

test('remove deletes only the matching deadline', () => {
  const list = [deadline(), deadline({ id: 'k7m2', date: '2027-03-19' })];
  const out = dw.applyCommand(list, {
    text: 'remove p4x9',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, true);
  assert.deepEqual(out.list.map(d => d.id), ['k7m2']);
  assert.match(out.text, /Removed {2}May 23, 2027 {2}\[p4x9\]/);
});

test('rm and delete are accepted aliases', () => {
  for (const alias of ['rm', 'delete']) {
    const out = dw.applyCommand([deadline()], {
      text: `${alias} p4x9`,
      channel: CH_A,
      todayISO: '2026-09-09'
    });
    assert.equal(out.list.length, 0, `${alias} should remove`);
  }
});

test('remove rejects an unknown id', () => {
  const out = dw.applyCommand([deadline()], {
    text: 'remove nope',
    channel: CH_A,
    todayISO: '2026-09-09'
  });
  assert.equal(out.changed, false);
  assert.equal(out.list.length, 1);
});

test('clear and reset are no longer supported and fall through to help', () => {
  for (const gone of ['clear', 'reset']) {
    const out = dw.applyCommand([deadline()], {
      text: gone,
      channel: CH_A,
      todayISO: '2026-09-09'
    });
    assert.equal(out.changed, false);
    assert.equal(out.list.length, 1);
    assert.match(out.text, /DeadlineWatcher/);
  }
});

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

test('parseCommand splits subcommand from arguments', () => {
  assert.deepEqual(dw.parseCommand('  add   2027-05-23   VSS  paper '), {
    sub: 'add',
    args: ['2027-05-23', 'VSS', 'paper']
  });
});

test('an empty command shows help', () => {
  assert.equal(dw.parseCommand('').sub, 'help');
  const out = dw.applyCommand([], { text: '', channel: CH_A, todayISO: '2026-09-09' });
  assert.match(out.text, /\/deadline add <YYYY-MM-DD> <title>/);
});

test('an unknown subcommand shows help', () => {
  const out = dw.applyCommand([], { text: 'frobnicate', channel: CH_A, todayISO: '2026-09-09' });
  assert.match(out.text, /DeadlineWatcher/);
});

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

test('records survive save/load with all fields intact', () => {
  const file = tmpFile('deadlines.json');
  const list = [deadline({ title: '<@U123> VSS paper deadline' })];
  dw.saveDeadlines(list, file);
  assert.deepEqual(dw.loadDeadlines(file), list);
});

test('loadDeadlines does not discard unrecognised fields', () => {
  const file = tmpFile('deadlines.json');
  fs.writeFileSync(file, JSON.stringify([deadline({ futureField: 42 })]));
  assert.equal(dw.loadDeadlines(file)[0].futureField, 42);
});

test('loadDeadlines returns an empty list for a missing or broken file', () => {
  assert.deepEqual(dw.loadDeadlines(tmpFile('missing.json')), []);
  const broken = tmpFile('broken.json');
  fs.writeFileSync(broken, 'not json');
  assert.deepEqual(dw.loadDeadlines(broken), []);
});

test('pruneExpired removes only records past their date', () => {
  const list = [
    deadline({ id: 'old1', date: '2026-09-08' }),
    deadline({ id: 'today', date: '2026-09-09' }),
    deadline({ id: 'soon', date: '2026-09-10' })
  ];
  assert.deepEqual(dw.pruneExpired(list, '2026-09-09').map(d => d.id), ['today', 'soon']);
});

// ---------------------------------------------------------------------------
// Daily check
// ---------------------------------------------------------------------------

test('check prunes expired records, persists them, and posts due reminders', async () => {
  const file = tmpFile('deadlines.json');
  dw.saveDeadlines(
    [
      deadline({ id: 'old1', date: '2026-09-08', title: 'Gone' }),
      deadline({ id: 'due1', date: '2026-12-09', title: '<@U123> VSS paper deadline' }),
      deadline({ id: 'quiet', date: '2027-01-01', title: 'Not due today' })
    ],
    file
  );

  const posted = [];
  const result = await dw.runCheck({
    todayISO: '2026-09-09',
    file,
    post: async (channel, text) => posted.push({ channel, text })
  });

  assert.equal(result.pruned, 1);
  assert.deepEqual(posted, [
    {
      channel: CH_A,
      text: '📅 3 months until <@U123> VSS paper deadline\nDeadline: Dec 9, 2026'
    }
  ]);
  assert.deepEqual(dw.loadDeadlines(file).map(d => d.id), ['due1', 'quiet']);
});

test('check does not rewrite the file when nothing expired', async () => {
  const file = tmpFile('deadlines.json');
  dw.saveDeadlines([deadline()], file);
  const before = fs.statSync(file).mtimeMs;
  await dw.runCheck({ todayISO: '2026-09-09', file, post: async () => {} });
  assert.equal(fs.statSync(file).mtimeMs, before);
});

test('an expired deadline is pruned before it can be reminded about', async () => {
  const file = tmpFile('deadlines.json');
  // Milestone date would match, but the record is already expired.
  dw.saveDeadlines([deadline({ date: '2026-09-08' })], file);
  const posted = [];
  await dw.runCheck({
    todayISO: '2026-09-09',
    file,
    post: async (channel, text) => posted.push({ channel, text })
  });
  assert.deepEqual(posted, []);
  assert.deepEqual(dw.loadDeadlines(file), []);
});

test('a failing post does not abort the rest of the check', async () => {
  const file = tmpFile('deadlines.json');
  dw.saveDeadlines(
    [
      deadline({ id: 'aaa1', date: '2026-12-09', channel: CH_A }),
      deadline({ id: 'bbb1', date: '2026-12-09', channel: CH_B })
    ],
    file
  );
  const posted = [];
  await dw.runCheck({
    todayISO: '2026-09-09',
    file,
    post: async channel => {
      if (channel === CH_A) throw new Error('not_in_channel');
      posted.push(channel);
    }
  });
  assert.deepEqual(posted, [CH_B]);
});

test('reminder labels use singular and plural wording', () => {
  const d = deadline({ date: '2027-05-23', title: 'VSS paper deadline' });
  const labels = ['2027-02-23', '2027-04-23', '2027-05-09', '2027-05-16'].map(
    day => dw.dueReminders([d], day)[0].text.split(' until ')[0]
  );
  assert.deepEqual(labels, ['📅 3 months', '📅 1 month', '📅 2 weeks', '📅 1 week']);
});
