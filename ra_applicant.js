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

const FORM_LABELS = { name: '', classYear: '', major: '' };

const INSTRUCTIONS = `You brief the PI of the Bionic Vision Lab on a new undergraduate research
assistant application.

Base every claim on the supplied application. Never invent experience, courses,
tools, participants, or responsibilities. Where the application is silent, say
so.

self_reported_skills is a grid the applicant ticked about themselves. It is a
claim. The written answers are the evidence. Rate each skill on how far the
prose backs the claim:

- "substantial": they describe doing it themselves, with enough specifics
  (tools, decisions, difficulties, scale) to be credible.
- "some": real hands-on contact, but partial, assisted, or vague about what
  they personally did.
- "exposure": coursework, club attendance, a workshop, a tutorial, or watching
  others.
- "unsupported": ticked in the grid, absent from the written answers.

Separate operating an existing setup from building it, and assisting on a
project from owning a responsibility. Coursework is not research expertise.

Cover a skill only where you have something to say about it. Skip the rest.

dependability rests on concrete evidence: a commitment kept, a schedule held, a
mistake caught, a handoff done properly. Tone and enthusiasm are not evidence.

headline: the most useful thing for a PI to know after reading the whole
application. One sentence.

limitations_or_uncertainties: gaps, contradictions between the grid and the
prose, unsupported claims, and practical constraints such as availability or
graduation date.

questions_to_clarify: short factual questions that would resolve a specific
uncertainty.

Do not score, rate, rank, or compare the applicant to anyone. Do not recommend
hiring, rejecting, interviewing, or matching them to a project. Do not infer
ability or reliability from name, nationality, gender, race, ethnicity,
disability, or any other demographic characteristic. Class year and major are
context, not a proxy for ability.

Be specific and brief.`;

/** The three bullet-list sections share one shape. */
function stringList(description) {
  return {
    type: 'array', maxItems: MAX_POINTS, items: { type: 'string' }, description
  };
}

const SCHEMA = {
  type: 'object',
  properties: {
    headline: {
      type: 'string',
      description: 'One sentence, the most useful takeaway for a PI.'
    },
    skills: {
      type: 'array',
      maxItems: MAX_SKILLS,
      description: 'Only skills worth a sentence, not one entry per grid row.',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string', description: 'The skill area, usually a grid row.' },
          evidence_level: { type: 'string', enum: SKILL_LEVELS },
          summary: {
            type: 'string',
            description: 'The evidence, and how it compares with the claim.'
          }
        },
        required: ['name', 'evidence_level', 'summary'],
        additionalProperties: false
      }
    },
    standout_evidence: stringList(
      'Concrete things that distinguish this application; empty if none do.'),
    limitations_or_uncertainties: stringList(
      'Gaps, contradictions, unsupported claims, or practical constraints.'),
    dependability: {
      type: 'object',
      properties: {
        evidence_level: { type: 'string', enum: DEPENDABILITY_LEVELS },
        summary: {
          type: 'string',
          description: 'What the applicant demonstrated, citing the response.'
        }
      },
      required: ['evidence_level', 'summary'],
      additionalProperties: false
    },
    questions_to_clarify: stringList('Short factual follow-up questions.')
  },
  required: ['headline', 'skills', 'standout_evidence',
    'limitations_or_uncertainties', 'dependability', 'questions_to_clarify'],
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
    if (!text || key === 'timestamp') continue;

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

  const headline = trimWords(result.headline, MAX_HEADLINE_WORDS);
  // Without a takeaway there is no briefing.
  if (!headline) return null;

  const skills = [];
  const seen = new Set();
  for (const skill of result.skills || []) {
    if (!skill || typeof skill !== 'object') continue;
    const name = sanitize(skill.name);
    const level = String(skill.evidence_level || '').trim().toLowerCase();
    const summary = trimWords(skill.summary, MAX_SUMMARY_WORDS);
    const key = name.toLowerCase();
    if (!name || !summary || !SKILL_LEVELS.includes(level) || seen.has(key)) continue;
    seen.add(key);
    skills.push({ name, evidence_level: level, summary });
    if (skills.length >= MAX_SKILLS) break;
  }

  let dependability = null;
  const raw = result.dependability;
  if (raw && typeof raw === 'object') {
    const level = String(raw.evidence_level || '').trim().toLowerCase();
    const summary = trimWords(raw.summary, MAX_SUMMARY_WORDS);
    if (DEPENDABILITY_LEVELS.includes(level) && summary) {
      dependability = { evidence_level: level, summary };
    }
  }

  return {
    headline,
    skills,
    standout_evidence: points(result.standout_evidence),
    limitations_or_uncertainties: points(result.limitations_or_uncertainties),
    dependability,
    questions_to_clarify: points(result.questions_to_clarify)
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
  const app = application || {};
  let text = header(app);
  if (!analysis) return text;

  text += `\n*Takeaway:* ${analysis.headline}\n`;

  const skills = (analysis.skills || []).slice().sort((a, b) =>
    SKILL_LEVELS.indexOf(a.evidence_level) - SKILL_LEVELS.indexOf(b.evidence_level));
  text += section('Demonstrated skills', skills.map(s =>
    `${MARKERS[s.evidence_level]} *${s.name}* — ${s.summary}`));

  text += section('Stands out',
    (analysis.standout_evidence || []).map(p => `• ${p}`));
  text += section('Gaps / things to clarify',
    (analysis.limitations_or_uncertainties || [])
      .concat(analysis.questions_to_clarify || []).map(p => `• ${p}`));

  if (analysis.dependability) {
    const { evidence_level: level, summary } = analysis.dependability;
    text += section('Dependability', [`${MARKERS[level]} ${summary}`]);
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
