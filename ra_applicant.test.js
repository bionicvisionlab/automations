const test = require('node:test');
const assert = require('node:assert/strict');

const ra = require('./ra_applicant');

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

// Google Forms hands the trigger {"Question": ["answer"]}.
function namedValues(over = {}) {
  return {
    Timestamp: ['2026-09-19 10:04:11'],
    'Full name': ['Jane Doe'],
    'Class year': ['3rd-year'],
    Major: ['PBS'],
    'Which do you have experience with? [Human subjects research]': ['Yes'],
    'Which do you have experience with? [Eye tracking]': ['Yes'],
    'Which do you have experience with? [EEG/BCI]': ['No'],
    'Which do you have experience with? [Unity/VR/AR]': [''],
    'Describe one project or responsibility.': ['I ran 40 participants.'],
    'Tell us about a time that shows how dependable you are.': ['I showed up.'],
    ...over
  };
}

// 1. Real human-subjects and psychophysics experience, with tools,
//    difficulties and scale.
const EXPERIENCED = {
  name: 'Jane Doe',
  classYear: '3rd-year',
  major: 'PBS',
  selfReportedSkills: {
    'Human subjects research': 'Yes',
    'Psychophysics software': 'Yes',
    'Eye tracking': 'Yes',
    'ML/AI models': 'Yes'
  },
  responses: {
    'Describe one project or responsibility.':
      'In the Example Perception Lab I modified a PsychoPy contrast detection ' +
      'task, synchronized it with an EyeLink 1000, and independently scheduled ' +
      'and ran about 40 participants. The hard part was a 12 ms trigger lag, ' +
      'which I tracked down with a photodiode.',
    'Tell us about a time that shows how dependable you are.':
      'I held the same Tuesday 8am testing slot for two quarters and wrote a ' +
      'handoff document before going on exchange.'
  }
};

// 2. Enthusiastic, with coursework, a club and a tutorial behind it.
const COURSEWORK = {
  name: 'Alex Roe',
  classYear: '1st-year',
  major: 'CS',
  selfReportedSkills: {
    Programming: 'Yes',
    'EEG/BCI': 'Yes',
    'Blind/clinical populations': 'Yes'
  },
  responses: {
    'Describe one project or responsibility.':
      'I took an intro programming course and built a number guessing game. ' +
      'I also attended BCI club meetings and followed an online tutorial.',
    'Tell us about a time that shows how dependable you are.':
      'I am very passionate about vision research and always try my best.'
  }
};

// A plausible model result. These fixtures protect the shape; no test asserts
// the prose.
const BRIEFING = {
  headline: 'Concrete psychophysics experience; programming evidence is ' +
    'thinner than the grid suggests.',
  skills: [
    {
      name: 'Human subjects research',
      evidence_level: 'substantial',
      summary: 'Independently scheduled and ran about 40 participants.'
    },
    {
      name: 'Eye tracking',
      evidence_level: 'some',
      summary: 'Synchronized an EyeLink 1000; unclear whether they configured it.'
    },
    {
      name: 'ML/AI models',
      evidence_level: 'unsupported',
      summary: 'Selected in the grid but absent from the written answers.'
    }
  ],
  standout_evidence: ['Diagnosed a 12 ms display-to-tracker lag with a photodiode.'],
  limitations_or_uncertainties: ['Graduates in June.'],
  dependability: {
    evidence_level: 'substantial',
    summary: 'Held the same 8am slot for two quarters and wrote a handoff document.'
  },
  questions_to_clarify: ['Did they write the synchronization code themselves?']
};

/** A UrlFetchApp.fetch stand-in that records calls and replays a reply. */
function fakeFetch({ result, body, status = 200, error, text } = {}) {
  const calls = [];
  const fetcher = (url, params) => {
    calls.push({ url, params });
    if (error) throw error;
    const payload = body !== undefined ? body
      : { output: [{ content: [{ type: 'output_text', text: text !== undefined ? text : JSON.stringify(result) }] }] };
    return {
      getResponseCode: () => status,
      getContentText: () => (typeof payload === 'string' ? payload : JSON.stringify(payload))
    };
  };
  fetcher.calls = calls;
  return fetcher;
}

function analyze(application, fetcher) {
  return ra.analyzeApplication(application, fetcher, 'test-key');
}

function sentBody(fetcher, call = 0) {
  return JSON.parse(fetcher.calls[call].params.payload);
}

function sentPayload(fetcher, call = 0) {
  return JSON.parse(sentBody(fetcher, call).input[1].content);
}

function briefing(over = {}) {
  return ra.cleanAnalysis({ ...BRIEFING, ...over });
}

// ---------------------------------------------------------------------------
// Normalization
// ---------------------------------------------------------------------------

test('named values unwrap to plain strings', () => {
  const app = ra.normalizeApplication(namedValues());
  assert.equal(app.responses['Describe one project or responsibility.'],
    'I ran 40 participants.');
});

test('timestamp is dropped entirely', () => {
  const app = ra.normalizeApplication(namedValues());
  assert.equal(JSON.stringify(app).includes('2026-09-19'), false);
});

test('grid columns become self-reported skills keyed by row', () => {
  const app = ra.normalizeApplication(namedValues());
  assert.deepEqual(app.selfReportedSkills,
    { 'Human subjects research': 'Yes', 'Eye tracking': 'Yes' });
});

test('unselected and blank grid rows are not claims', () => {
  const app = ra.normalizeApplication(namedValues());
  assert.equal('EEG/BCI' in app.selfReportedSkills, false);
  assert.equal('Unity/VR/AR' in app.selfReportedSkills, false);
});

test('name, class year and major are lifted out of the responses', () => {
  const app = ra.normalizeApplication(namedValues());
  assert.equal(app.name, 'Jane Doe');
  assert.equal(app.classYear, '3rd-year');
  assert.equal(app.major, 'PBS');
  assert.deepEqual(Object.keys(app.responses), [
    'Describe one project or responsibility.',
    'Tell us about a time that shows how dependable you are.'
  ]);
});

test('a question merely containing "year" stays a response', () => {
  const app = ra.normalizeApplication({
    'What year did you start at UCSB?': ['2024'],
    'Describe one project.': ['Something real.']
  });
  assert.equal(app.classYear, '');
  assert.equal(app.responses['What year did you start at UCSB?'], '2024');
});

test('explicit labels override the aliases', () => {
  const app = ra.normalizeApplication(
    { 'What is your full name?': ['Jane Doe'], 'Describe one project.': ['Real.'] },
    { name: 'What is your full name?', classYear: '', major: '' });
  assert.equal(app.name, 'Jane Doe');
  assert.equal('What is your full name?' in app.responses, false);
});

test('checkbox answers join into one string', () => {
  const app = ra.normalizeApplication({
    'Q [Programming]': ['Python', 'MATLAB'],
    'Describe one project.': ['Real.']
  });
  assert.equal(app.selfReportedSkills.Programming, 'Python, MATLAB');
});

// ---------------------------------------------------------------------------
// What goes to OpenAI
// ---------------------------------------------------------------------------

test('the request is not stored', () => {
  assert.equal(ra.buildOpenAIRequest(EXPERIENCED).store, false);
});

test('the request uses the fixed model settings and no tools', () => {
  const body = ra.buildOpenAIRequest(EXPERIENCED);
  assert.equal(body.model, 'gpt-5.6');
  assert.deepEqual(body.reasoning, { effort: 'low' });
  assert.equal('tools' in body, false);
});

test('the applicant name never reaches OpenAI', () => {
  const fetcher = fakeFetch({ result: BRIEFING });
  analyze(EXPERIENCED, fetcher);
  assert.equal(JSON.stringify(fetcher.calls[0]).includes('Jane Doe'), false);
});

test('prose answers and the claimed grid both reach OpenAI', () => {
  const fetcher = fakeFetch({ result: BRIEFING });
  analyze(EXPERIENCED, fetcher);
  const payload = sentPayload(fetcher);
  assert.match(
    payload.responses['Describe one project or responsibility.'], /photodiode/);
  assert.equal(payload.self_reported_skills['ML/AI models'], 'Yes');
});

test('class year and major are sent as context, and omitted when absent', () => {
  const payload = JSON.parse(ra.buildOpenAIRequest(EXPERIENCED).input[1].content);
  assert.equal(payload.class_year, '3rd-year');
  assert.equal(payload.major, 'PBS');

  const bare = JSON.parse(
    ra.buildOpenAIRequest({ responses: { Q: 'A real answer.' } }).input[1].content);
  assert.equal('class_year' in bare, false);
  assert.equal('major' in bare, false);
});

test('structured output is strict and carries the intended evidence levels', () => {
  const fmt = ra.buildOpenAIRequest(EXPERIENCED).text.format;
  assert.equal(fmt.type, 'json_schema');
  assert.equal(fmt.strict, true);

  const skill = fmt.schema.properties.skills.items;
  assert.deepEqual(skill.properties.evidence_level.enum,
    ['substantial', 'some', 'exposure', 'unsupported']);
  assert.deepEqual(fmt.schema.properties.dependability.properties.evidence_level.enum,
    ['substantial', 'some', 'limited', 'none']);

  // Strict structured output needs closed objects with every property required.
  assert.equal(fmt.schema.additionalProperties, false);
  assert.deepEqual(fmt.schema.required.sort(),
    Object.keys(fmt.schema.properties).sort());
  assert.equal(skill.additionalProperties, false);
  assert.deepEqual(skill.required.sort(), Object.keys(skill.properties).sort());
});

test('an application with no written answers makes no request', () => {
  const fetcher = fakeFetch({ result: BRIEFING });
  assert.equal(analyze({ ...EXPERIENCED, responses: { Q: '  ' } }, fetcher), null);
  assert.equal(fetcher.calls.length, 0);
});

test('a missing API key makes no request', () => {
  const fetcher = fakeFetch({ result: BRIEFING });
  assert.equal(ra.analyzeApplication(EXPERIENCED, fetcher, ''), null);
  assert.equal(fetcher.calls.length, 0);
});

// ---------------------------------------------------------------------------
// Parsing and failure
// ---------------------------------------------------------------------------

test('a valid response is parsed', () => {
  const analysis = analyze(EXPERIENCED, fakeFetch({ result: BRIEFING }));
  assert.equal(analysis.headline, BRIEFING.headline);
  assert.deepEqual(analysis.skills.map(s => s.name),
    ['Human subjects research', 'Eye tracking', 'ML/AI models']);
  assert.equal(analysis.dependability.evidence_level, 'substantial');
});

test('a refusal yields no briefing', () => {
  const refusal = { output: [{ content: [{ type: 'refusal', refusal: 'no' }] }] };
  assert.equal(ra.outputText(refusal), '');
  assert.equal(analyze(EXPERIENCED, fakeFetch({ body: refusal })), null);
});

test('malformed model output yields no briefing', () => {
  assert.equal(analyze(EXPERIENCED, fakeFetch({ text: '{not json' })), null);
});

test('a non-200 response yields no briefing', () => {
  assert.equal(analyze(EXPERIENCED, fakeFetch({ status: 429, body: {} })), null);
});

test('a thrown fetch yields no briefing, one attempt only', () => {
  const fetcher = fakeFetch({ error: new Error('boom') });
  assert.equal(analyze(EXPERIENCED, fetcher), null);
  assert.equal(fetcher.calls.length, 1);
});

test('failures log an error name, never applicant text', (t) => {
  const logged = [];
  t.mock.method(console, 'error', msg => logged.push(String(msg)));

  const err = new Error('Jane Doe ran 40 participants with a photodiode');
  err.name = 'HttpError';
  analyze(EXPERIENCED, fakeFetch({ error: err }));

  assert.deepEqual(logged, ['No RA briefing: HttpError']);
});

test('unusable results are rejected', () => {
  assert.equal(ra.cleanAnalysis(null), null);
  assert.equal(ra.cleanAnalysis('text'), null);
  assert.equal(ra.cleanAnalysis({ headline: '   ' }), null);
});

test('unknown levels, empty summaries and duplicates are dropped', () => {
  const analysis = ra.cleanAnalysis({
    headline: 'A takeaway.',
    skills: [
      { name: 'Programming', evidence_level: 'excellent', summary: 'Bad level.' },
      { name: 'Eye tracking', evidence_level: 'some', summary: '' },
      { name: 'EEG/BCI', evidence_level: 'exposure', summary: 'Club meetings.' },
      { name: 'eeg/bci', evidence_level: 'some', summary: 'Duplicate.' },
      'not an object'
    ],
    dependability: { evidence_level: 'unknown', summary: 'x' },
    standout_evidence: ['Same.', 'Same.']
  });
  assert.deepEqual(analysis.skills.map(s => s.name), ['EEG/BCI']);
  assert.equal(analysis.dependability, null);
  assert.deepEqual(analysis.standout_evidence, ['Same.']);
});

test('long prose and long lists are capped', () => {
  const long = Array(200).fill('word').join(' ');
  const analysis = ra.cleanAnalysis({
    headline: long,
    skills: [{ name: 'Programming', evidence_level: 'some', summary: long }],
    standout_evidence: Array(20).fill(0).map((_, i) => `Point ${i}.`)
  });
  assert.ok(analysis.headline.split(' ').length <= 31);
  assert.ok(analysis.skills[0].summary.split(' ').length <= 25);
  assert.equal(analysis.standout_evidence.length, ra.MAX_POINTS);
});

// ---------------------------------------------------------------------------
// Slack rendering
// ---------------------------------------------------------------------------

test('the header carries name, class year and major', () => {
  const text = ra.formatSlack(EXPERIENCED, briefing());
  assert.ok(text.startsWith('*New RA application — Jane Doe · 3rd-year PBS*\n'));
});

test('the header degrades when context is missing', () => {
  assert.ok(ra.formatSlack({ name: 'Jane Doe' }, briefing())
    .startsWith('*New RA application — Jane Doe*\n'));
  assert.ok(ra.formatSlack({}, briefing()).startsWith('*New RA application*\n'));
});

test('demonstrated and unsupported claims look different', () => {
  const text = ra.formatSlack(EXPERIENCED, briefing());
  const line = name => text.split('\n').find(l => l.includes(name));

  assert.ok(line('Human subjects research').startsWith(ra.MARKERS.substantial));
  assert.ok(line('ML/AI models').startsWith(ra.MARKERS.unsupported));
  assert.notEqual(ra.MARKERS.substantial, ra.MARKERS.unsupported);
  assert.ok(line('Eye tracking').startsWith(ra.MARKERS.some));
});

test('skills run from best evidenced to unsupported', () => {
  const text = ra.formatSlack(EXPERIENCED, briefing({
    skills: [
      { name: 'ML/AI models', evidence_level: 'unsupported', summary: 'Nothing.' },
      { name: 'EEG/BCI', evidence_level: 'exposure', summary: 'Club.' },
      { name: 'Human subjects research', evidence_level: 'substantial', summary: 'Ran 40.' }
    ]
  }));
  const at = name => text.indexOf(name);
  assert.ok(at('Human subjects research') < at('EEG/BCI'));
  assert.ok(at('EEG/BCI') < at('ML/AI models'));
});

test('empty sections are omitted', () => {
  const text = ra.formatSlack(COURSEWORK, briefing({
    skills: [],
    standout_evidence: [],
    limitations_or_uncertainties: [],
    questions_to_clarify: [],
    dependability: null
  }));
  for (const heading of ['Demonstrated skills', 'Stands out',
    'Gaps / things to clarify', 'Dependability']) {
    assert.equal(text.includes(heading), false, heading);
  }
  assert.ok(text.includes('*Takeaway:*'));
});

test('gaps and clarifying questions share one section', () => {
  const text = ra.formatSlack(EXPERIENCED, briefing());
  assert.equal(text.split('*Gaps / things to clarify*').length - 1, 1);
  assert.ok(text.includes('Graduates in June.'));
  assert.ok(text.includes('Did they write the synchronization code themselves?'));
});

test('model prose cannot inject Slack markup', () => {
  const text = ra.formatSlack(EXPERIENCED, ra.cleanAnalysis({
    headline: 'Ping <!channel> & <@U123> now.',
    skills: [{ name: '<@U999>', evidence_level: 'some', summary: 'See <http://x|y>.' }]
  }));
  assert.equal(text.includes('<!channel>'), false);
  assert.equal(text.includes('<@U123>'), false);
  assert.equal(text.includes('<@U999>'), false);
  assert.ok(text.includes('&lt;!channel&gt;'));
});

test('no analysis still announces the application', () => {
  assert.equal(ra.formatSlack(EXPERIENCED, null),
    '*New RA application — Jane Doe · 3rd-year PBS*\n');
});

test('an application URL appears when supplied', () => {
  const url = 'https://docs.google.com/spreadsheets/d/x#gid=0&range=A7';
  assert.ok(ra.formatSlack(EXPERIENCED, briefing(), url)
    .includes(`<${url}|Open application>`));
  assert.equal(ra.formatSlack(EXPERIENCED, briefing()).includes('Open application'),
    false);
});

test('a non-http or markup-bearing URL is dropped', () => {
  for (const url of ['javascript:alert(1)', 'https://x|<@U123>', 'not a url']) {
    assert.equal(
      ra.formatSlack(EXPERIENCED, briefing(), url).includes('Open application'),
      false, url);
  }
});

test('a realistic briefing is short', () => {
  const text = ra.formatSlack(EXPERIENCED, briefing());
  assert.ok(text.length <= 1200, `length ${text.length}`);
  assert.ok(text.split('\n').length <= 20);
});

// A model that pads every field still cannot produce an unbounded message.
// Raising MAX_SKILLS or any word cap has to move this number too.
test('a padded briefing stays bounded', () => {
  const long = Array(60).fill('wordy').join(' ');
  const text = ra.formatSlack(EXPERIENCED, ra.cleanAnalysis({
    headline: long,
    skills: Array(15).fill(0).map((_, i) => (
      { name: `Skill name ${i}`, evidence_level: 'substantial', summary: long })),
    standout_evidence: Array(10).fill(0).map((_, i) => `${i} ${long}`),
    limitations_or_uncertainties: Array(10).fill(0).map((_, i) => `${i} ${long}`),
    questions_to_clarify: Array(10).fill(0).map((_, i) => `${i} ${long}`),
    dependability: { evidence_level: 'some', summary: long }
  }));
  assert.ok(text.length <= 3200, `length ${text.length}`);
  assert.ok(text.split('\n').length <= 32, `lines ${text.split('\n').length}`);
});

// ---------------------------------------------------------------------------
// Fixtures end to end
// ---------------------------------------------------------------------------

test('the experienced fixture sends its own evidence', () => {
  const fetcher = fakeFetch({ result: BRIEFING });
  analyze(EXPERIENCED, fetcher);
  assert.match(JSON.stringify(sentPayload(fetcher)), /photodiode/);
});

test('the coursework fixture renders exposure and unsupported claims', () => {
  const fetcher = fakeFetch({ result: BRIEFING });
  analyze(COURSEWORK, fetcher);
  assert.match(JSON.stringify(sentPayload(fetcher)), /BCI club meetings/);

  const text = ra.formatSlack(COURSEWORK, ra.cleanAnalysis({
    headline: 'Evidence is coursework and club attendance rather than research.',
    skills: [
      { name: 'Programming', evidence_level: 'exposure', summary: 'One intro course.' },
      {
        name: 'Blind/clinical populations',
        evidence_level: 'unsupported',
        summary: 'Ticked in the grid, absent from the answers.'
      }
    ],
    dependability: {
      evidence_level: 'none',
      summary: 'The answer describes enthusiasm, not a kept commitment.'
    }
  }));

  assert.ok(text.includes('*New RA application — Alex Roe · 1st-year CS*'));
  assert.ok(text.includes(`${ra.MARKERS.exposure} *Programming*`));
  assert.ok(text.includes(`${ra.MARKERS.unsupported} *Blind/clinical populations*`));
  assert.equal(text.includes('Stands out'), false);
  assert.ok(text.includes('*Dependability*'));
});

// ---------------------------------------------------------------------------
// Slack delivery
// ---------------------------------------------------------------------------

test('the Slack webhook receives the rendered text as JSON', () => {
  const fetcher = fakeFetch({ body: {} });
  const text = ra.formatSlack(EXPERIENCED, briefing());
  assert.equal(ra.postToSlack('https://hooks.slack.com/services/x', text, fetcher), true);

  const { url, params } = fetcher.calls[0];
  assert.equal(url, 'https://hooks.slack.com/services/x');
  assert.equal(params.method, 'post');
  assert.equal(JSON.parse(params.payload).text, text);
});

test('a missing webhook posts nothing', () => {
  const fetcher = fakeFetch({ body: {} });
  assert.equal(ra.postToSlack('', 'text', fetcher), false);
  assert.equal(fetcher.calls.length, 0);
});
