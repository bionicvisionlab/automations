// ra_applicant.js
//
// Summarize a new undergraduate RA application into one Slack briefing.
//
// Runs inside Google Apps Script, bound to the Form's response Sheet:
//
//   Form submit -> handleFormSubmit(e) -> normalizeApplication(e.namedValues)
//     -> OpenAI Responses API -> formatSlack() -> Slack incoming webhook
//
// Everything above the "Apps Script edge" banner runs under `node --test`.
// UrlFetchApp, PropertiesService and SpreadsheetApp appear only below it.
//
// The briefing reports what the written answers demonstrate. It does not score,
// rank, or recommend. Applications are personal data: requests set store:false
// and nothing here logs or stores one.

const OPENAI_URL = 'https://api.openai.com/v1/responses';
const MODEL = 'gpt-5.6';

// How far the written answers back a claimed skill.
const SKILL_LEVELS = ['substantial', 'some', 'exposure', 'unsupported'];
const DEPENDABILITY_LEVELS = ['substantial', 'some', 'limited', 'none'];

const MARKERS = {
  substantial: '\u{1F7E2}',
  some: '\u{1F7E1}',
  exposure: '\u{1F7E0}',
  limited: '\u{1F7E0}',
  unsupported: '⚪',
  none: '⚪'
};

// Caps that keep the Slack message readable on a phone.
const MAX_SKILLS = 9;
const MAX_POINTS = 3;
const MAX_HEADLINE_WORDS = 30;
const MAX_SUMMARY_WORDS = 24;
const MAX_POINT_WORDS = 20;

// Grid answers that mean "not selected".
const NEGATIVE_ANSWERS = [
  'no', 'none', 'n/a', 'na', 'never', 'false', 'unchecked', '0',
  'no experience', 'not at all'
];

// Header fields, matched on the whole question label so that "What year did
// you start?" does not read as a class year. Set FORM_LABELS once the live
// Sheet headers are known and these are bypassed.
const NAME_ALIASES = ['name', 'full name', 'your name', 'your full name'];
const CLASS_YEAR_ALIASES = ['class year', 'year in school', 'academic year',
  'year of study'];
const MAJOR_ALIASES = ['major', 'your major', 'field of study'];

const FORM_LABELS = {
  name: 'Name',
  classYear: 'What is your current year at UCSB?',
  major: 'Major'
};

const IGNORED_LABELS = new Set([
  'timestamp',
  'email address',
  'cumulative gpa',
  'please upload a pdf of your cv or resume (yourname_resume.pdf)',
  'please upload a non-official transcript (pdf format; yourname_transcript.pdf)'
]);

const INSTRUCTIONS = `Brief the PI of the Bionic Vision Lab on an undergraduate RA application.

Be highly selective and concise. Do not rewrite the application.

self_reported_skills is self-report, not evidence. Use the written responses to
classify relevant skills as:
- substantial: clear hands-on responsibility with concrete specifics
- some: real hands-on experience, but partial or assisted
- exposure: coursework, clubs, tutorials, workshops, or observing
- unsupported: claimed but not backed by written evidence

Return at most 5 skill labels. Skill names must be SHORT LABELS ONLY, with no
explanation or qualification.

highlights: at most 3 short bullets containing the most useful concrete facts
from the whole application. Include technical/research experience,
dependability, availability, or unusually relevant prior experience when
important. Do not repeat facts.

gaps_or_questions: at most 2 short bullets containing only the most important
uncertainty or follow-up question.

If the applicant mentions VIU or the Vision and Image Understanding Lab,
recognize it as Miguel Eckstein's vision research lab and treat involvement
there as relevant vision-research experience, while distinguishing affiliation
from demonstrated responsibilities.

Prefer concrete evidence over interpretation. Omit minor caveats and missing
details.

Do not score, rank, compare, or make hiring/interview recommendations.`;

/** The three bullet-list sections share one shape. */
function stringList(description) {
  return {
    type: 'array', maxItems: MAX_POINTS, items: { type: 'string' }, description
  };
}

const SCHEMA = {
  type: 'object',
  properties: {
    skills: {
      type: 'array',
      maxItems: 5,
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          evidence_level: { type: 'string', enum: SKILL_LEVELS }
        },
        required: ['name', 'evidence_level'],
        additionalProperties: false
      }
    },
    highlights: {
      type: 'array',
      maxItems: 3,
      items: { type: 'string' }
    },
    gaps_or_questions: {
      type: 'array',
      maxItems: 2,
      items: { type: 'string' }
    }
  },
  required: ['skills', 'highlights', 'gaps_or_questions'],
  additionalProperties: false
};

// ---------------------------------------------------------------------------
// Normalization
// ---------------------------------------------------------------------------

const GRID_LABEL = /^\s*(.*?)\s*\[\s*(.+?)\s*\]\s*$/;

// e.namedValues gives {"Question": ["answer"]}; a checkbox question gives
// several entries in that array.
function answerText(value) {
  if (Array.isArray(value)) {
    return value.map(answerText).filter(Boolean).join(', ');
  }
  if (value === null || value === undefined) return '';
  return String(value).split(/\s+/).filter(Boolean).join(' ');
}

function labelKey(label) {
  return answerText(label).replace(/[?:*]+$/, '').trim().toLowerCase();
}

/**
 * Turn Google Forms named values into an application object.
 *
 * Grid questions arrive as one column per row, labelled "Question [Row]", and
 * become selfReportedSkills keyed by the row label; answers meaning "not
 * selected" are dropped, as is Timestamp. Name, class year and major go to the
 * Slack header, matched against `labels` when the integration supplies them
 * and against the aliases otherwise. Every other question keeps its text
 * verbatim under `responses`.
 */
function normalizeApplication(fields, labels) {
  const explicit = labels || FORM_LABELS;
  const wanted = [
    ['name', labelKey(explicit.name), NAME_ALIASES],
    ['classYear', labelKey(explicit.classYear), CLASS_YEAR_ALIASES],
    ['major', labelKey(explicit.major), MAJOR_ALIASES]
  ];

  const application = {
    name: '', classYear: '', major: '', selfReportedSkills: {}, responses: {}
  };

  for (const [label, value] of Object.entries(fields || {})) {
    const text = answerText(label);
    const key = labelKey(label);
    const answer = answerText(value);
    if (!text || IGNORED_LABELS.has(key)) continue;

    const grid = GRID_LABEL.exec(text);
    if (grid) {
      if (answer && !NEGATIVE_ANSWERS.includes(answer.toLowerCase())) {
        application.selfReportedSkills[grid[2]] = answer;
      }
      continue;
    }

    if (!answer) continue;

    const field = wanted.find(([name, exact, aliases]) =>
      !application[name] && (exact ? key === exact : aliases.includes(key)));
    if (field) application[field[0]] = answer;
    else application.responses[text] = answer;
  }

  return application;
}

// ---------------------------------------------------------------------------
// OpenAI
// ---------------------------------------------------------------------------

/** The exact Responses API body. The applicant's name is never included. */
function buildOpenAIRequest(application) {
  const payload = {
    self_reported_skills: application.selfReportedSkills || {},
    responses: application.responses || {}
  };
  if (application.classYear) payload.class_year = application.classYear;
  if (application.major) payload.major = application.major;

  return {
    model: MODEL,
    reasoning: { effort: 'low' },
    // The request carries applicant personal data; do not store it.
    store: false,
    input: [
      { role: 'system', content: INSTRUCTIONS },
      { role: 'user', content: JSON.stringify(payload) }
    ],
    text: {
      format: {
        type: 'json_schema',
        name: 'ra_application_briefing',
        strict: true,
        schema: SCHEMA
      }
    }
  };
}

/** The assistant text of a Responses API reply. A refusal yields ''. */
function outputText(body) {
  const parts = [];
  for (const item of (body && body.output) || []) {
    for (const chunk of item.content || []) {
      if (chunk.type === 'output_text' && chunk.text) parts.push(chunk.text);
    }
  }
  return parts.join('').trim();
}

/**
 * Ask OpenAI what an application demonstrates.
 *
 * `fetchFn` has the UrlFetchApp.fetch signature and defaults to it in
 * production. Returns null on any problem: no written answers, no API key, an
 * HTTP error, a refusal or an unusable result. One attempt, and failures log
 * an error name only.
 */
function analyzeApplication(application, fetchFn, apiKey) {
  const responses = (application && application.responses) || {};
  // The grid is self-report with nothing to check it against.
  if (!Object.values(responses).some(answerText)) return null;

  const fetcher = fetchFn ||
    (typeof UrlFetchApp === 'undefined' ? null : UrlFetchApp.fetch);
  if (!fetcher || !apiKey) return null;

  try {
    const response = fetcher(OPENAI_URL, {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: `Bearer ${apiKey}` },
      payload: JSON.stringify(buildOpenAIRequest(application)),
      muteHttpExceptions: true
    });

    const status = response.getResponseCode();
    if (status !== 200) {
      console.error(`No RA briefing: OpenAI HTTP ${status}`);
      return null;
    }

    const text = outputText(JSON.parse(response.getContentText()));
    if (!text) {
      console.error('No RA briefing: empty model output');
      return null;
    }
    return cleanAnalysis(JSON.parse(text));
  } catch (err) {
    console.error(`No RA briefing: ${err.name}`);
    return null;
  }
}

/**
 * Validate a model result into a briefing object, or null if unusable.
 *
 * The strict schema already constrains shape and enums. This checks them
 * again anyway, trims length, and drops empties.
 */
function cleanAnalysis(result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return null;

  const skills = [];
  const seen = new Set();

  for (const skill of result.skills || []) {
    if (!skill || typeof skill !== 'object') continue;

    const name = sanitize(skill.name);
    const level = String(skill.evidence_level || '').trim().toLowerCase();
    const key = name.toLowerCase();

    if (!name || !SKILL_LEVELS.includes(level) || seen.has(key)) continue;

    seen.add(key);
    skills.push({ name, evidence_level: level });

    if (skills.length >= 5) break;
  }

  const highlights = (result.highlights || [])
    .filter(x => typeof x === 'string' && sanitize(x))
    .slice(0, 3)
    .map(sanitize);

  const gaps = (result.gaps_or_questions || [])
    .filter(x => typeof x === 'string' && sanitize(x))
    .slice(0, 2)
    .map(sanitize);

  if (!skills.length && !highlights.length && !gaps.length) return null;

  return {
    skills,
    highlights,
    gaps_or_questions: gaps
  };
}

// ---------------------------------------------------------------------------
// Slack rendering
// ---------------------------------------------------------------------------

/**
 * Collapse text to one inert line of Slack mrkdwn.
 *
 * Escaping &, < and > stops applicant or model text from becoming a mention,
 * link or @channel broadcast; Slack renders the entities literally.
 */
function sanitize(text) {
  return answerText(text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function trimWords(text, maxWords) {
  const words = sanitize(text).split(' ').filter(Boolean);
  if (words.length <= maxWords) return words.join(' ');
  return `${words.slice(0, maxWords).join(' ')} ...`;
}

function points(values) {
  const out = [];
  for (const value of values || []) {
    const point = typeof value === 'string' ? trimWords(value, MAX_POINT_WORDS) : '';
    if (point && !out.includes(point)) out.push(point);
    if (out.length >= MAX_POINTS) break;
  }
  return out;
}

/** "*New RA application — Jane Doe · 3rd-year PBS*". Absent fields drop out. */
function header(application) {
  const name = sanitize(application.name);
  const context = [application.classYear, application.major]
    .map(sanitize).filter(Boolean).join(' ');
  let title = name ? `New RA application — ${name}` : 'New RA application';
  if (context) title += ` · ${context}`;
  return `*${title}*\n`;
}

/** One Slack section, or '' when it has no lines. */
function section(heading, lines) {
  return lines.length ? `\n*${heading}*\n${lines.join('\n')}\n` : '';
}

/** An inert Slack link, or '' for anything but a plain http(s) URL. */
function link(url) {
  const clean = answerText(url);
  if (!/^https?:\/\//.test(clean) || /[<>|]/.test(clean)) return '';
  return `\n<${clean}|Open application>`;
}

/**
 * Render one briefing as a compact Slack message.
 *
 * All markup comes from here, never from the model. Empty sections are
 * dropped. With no analysis, the header alone announces the application.
 */
function formatSlack(application, analysis, applicationUrl) {
  const context = [application.name, application.major]
    .map(sanitize)
    .filter(Boolean)
    .join(' · ');

  let text = `*${context || 'RA application'}*\n`;

  if (!analysis) return text + link(applicationUrl);

  if (analysis.skills.length) {
    text += analysis.skills
      .map(s => `${MARKERS[s.evidence_level]} ${sanitize(s.name)}`)
      .join(' · ') + '\n';
  }

  if (analysis.highlights.length) {
    text += '\n*Highlights*\n';
    text += analysis.highlights.map(x => `• ${x}`).join('\n') + '\n';
  }

  if (analysis.gaps_or_questions.length) {
    text += '\n*Gaps / questions*\n';
    text += analysis.gaps_or_questions.map(x => `• ${x}`).join('\n') + '\n';
  }

  return text + link(applicationUrl);
}

// ---------------------------------------------------------------------------
// Apps Script edge
// ---------------------------------------------------------------------------

/** POST one message to a Slack incoming webhook. */
function postToSlack(webhookUrl, text, fetchFn) {
  const fetcher = fetchFn ||
    (typeof UrlFetchApp === 'undefined' ? null : UrlFetchApp.fetch);
  if (!fetcher || !webhookUrl || !text) return false;
  fetcher(webhookUrl, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({ text }),
    muteHttpExceptions: true
  });
  return true;
}

/** Deep link to the submitted row. */
function applicationUrl(e) {
  if (!e || !e.range || typeof SpreadsheetApp === 'undefined') return '';
  const sheet = e.range.getSheet();
  return `${sheet.getParent().getUrl()}#gid=${sheet.getSheetId()}` +
    `&range=A${e.range.getRow()}`;
}

/**
 * Installable on-form-submit trigger, set up in the response Sheet's Apps
 * Script project. Secrets live in Script Properties.
 */
function handleFormSubmit(e) {
  const props = PropertiesService.getScriptProperties();
  const application = normalizeApplication(e && e.namedValues);
  const analysis = analyzeApplication(
    application, UrlFetchApp.fetch, props.getProperty('OPENAI_API_KEY'));
  postToSlack(
    props.getProperty('SLACK_WEBHOOK_URL'),
    formatSlack(application, analysis, applicationUrl(e)),
    UrlFetchApp.fetch);
}

// Print what a Sheet row normalizes to, calling nothing:
//
//   node ra_applicant.js ra_application.example.json
//
if (typeof require !== 'undefined' && require.main === module) {
  const file = process.argv[2];
  if (!file) {
    console.error('usage: node ra_applicant.js <namedValues.json>');
    process.exit(1);
  }
  const fields = JSON.parse(require('fs').readFileSync(file, 'utf8'));
  console.log(JSON.stringify(normalizeApplication(fields), null, 2));
}

// Apps Script has no module system and skips this; Node picks it up.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    DEPENDABILITY_LEVELS,
    MARKERS,
    MAX_POINTS,
    SCHEMA,
    SKILL_LEVELS,
    analyzeApplication,
    buildOpenAIRequest,
    cleanAnalysis,
    formatSlack,
    normalizeApplication,
    outputText,
    postToSlack
  };
}