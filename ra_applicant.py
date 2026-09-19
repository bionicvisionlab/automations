#!/usr/bin/env python

"""
Summarize a new undergraduate RA application for the lab's Slack channel.

A Google Form feeds responses into a Sheet; an on-submit Apps Script trigger
hands one application to analyze_application(), and format_slack() renders the
result.

The briefing reports what the written answers demonstrate. It does not score,
rank, or recommend. Applications are personal data, so requests set store=False
and nothing here logs or stores one.
"""

import os
import re
import sys
import json

ANALYSIS_MODEL = 'gpt-5.6'
ANALYSIS_TIMEOUT = 30

# How far the written answers back a claimed skill.
SKILL_LEVELS = ('substantial', 'some', 'exposure', 'unsupported')
DEPENDABILITY_LEVELS = ('substantial', 'some', 'limited', 'none')

SKILL_MARKERS = {
    'substantial': '\N{LARGE GREEN CIRCLE}',
    'some': '\N{LARGE YELLOW CIRCLE}',
    'exposure': '\N{LARGE ORANGE CIRCLE}',
    'unsupported': '\N{MEDIUM WHITE CIRCLE}',
}

DEPENDABILITY_MARKERS = {
    'substantial': '\N{LARGE GREEN CIRCLE}',
    'some': '\N{LARGE YELLOW CIRCLE}',
    'limited': '\N{LARGE ORANGE CIRCLE}',
    'none': '\N{MEDIUM WHITE CIRCLE}',
}

# Caps that keep the Slack message readable on a phone.
MAX_SKILLS = 9
MAX_POINTS = 4
MAX_HEADLINE_WORDS = 35
MAX_SUMMARY_WORDS = 30
MAX_POINT_WORDS = 25

# Grid answers that mean "not selected".
NEGATIVE_ANSWERS = frozenset({
    'no', 'none', 'n/a', 'na', 'never', 'false', 'unchecked', '0',
    'no experience', 'not at all',
})

# Header fields the normalization helper lifts out of a flat Forms row.
NAME_HINTS = ('full name', 'your name', 'name')
CLASS_YEAR_HINTS = ('class year', 'year in school', 'academic year', 'year')
MAJOR_HINTS = ('major', 'field of study', 'department')

ANALYSIS_INSTRUCTIONS = """\
You brief the PI of the Bionic Vision Lab on a new undergraduate research
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

Be specific and brief.
"""

ANALYSIS_SCHEMA = {
    'type': 'object',
    'properties': {
        'headline': {
            'type': 'string',
            'description': "One sentence, the most useful takeaway for a PI.",
        },
        'skills': {
            'type': 'array',
            'maxItems': MAX_SKILLS,
            'items': {
                'type': 'object',
                'properties': {
                    'name': {
                        'type': 'string',
                        'description': "The skill area, usually a grid row.",
                    },
                    'evidence_level': {
                        'type': 'string',
                        'enum': list(SKILL_LEVELS),
                    },
                    'summary': {
                        'type': 'string',
                        'description': "The evidence, and how it compares "
                                       "with the claim.",
                    },
                },
                'required': ['name', 'evidence_level', 'summary'],
                'additionalProperties': False,
            },
            'description': "Only skills worth a sentence, not one entry per "
                           "grid row.",
        },
        'standout_evidence': {
            'type': 'array',
            'maxItems': MAX_POINTS,
            'items': {'type': 'string'},
            'description': "Concrete things that distinguish this "
                           "application; empty if none do.",
        },
        'limitations_or_uncertainties': {
            'type': 'array',
            'maxItems': MAX_POINTS,
            'items': {'type': 'string'},
            'description': "Gaps, contradictions, unsupported claims, or "
                           "practical constraints.",
        },
        'dependability': {
            'type': 'object',
            'properties': {
                'evidence_level': {
                    'type': 'string',
                    'enum': list(DEPENDABILITY_LEVELS),
                },
                'summary': {
                    'type': 'string',
                    'description': "What the applicant demonstrated, citing "
                                   "the response.",
                },
            },
            'required': ['evidence_level', 'summary'],
            'additionalProperties': False,
        },
        'questions_to_clarify': {
            'type': 'array',
            'maxItems': MAX_POINTS,
            'items': {'type': 'string'},
            'description': "Short factual follow-up questions.",
        },
    },
    'required': ['headline', 'skills', 'standout_evidence',
                 'limitations_or_uncertainties', 'dependability',
                 'questions_to_clarify'],
    'additionalProperties': False,
}


# --- input normalization ---------------------------------------------------

GRID_LABEL = re.compile(r'^\s*(?P<question>.*?)\s*\[\s*(?P<row>.+?)\s*\]\s*$')


def _clean_answer(value):
    """One form answer as collapsed text. Lists are comma-joined."""
    if isinstance(value, (list, tuple)):
        return ", ".join(_clean_answer(v) for v in value if _clean_answer(v))
    if value is None or isinstance(value, bool):
        return ''
    return " ".join(str(value).split())


def _matches(label, hints):
    """First hint that occurs in this question label, or None."""
    lowered = label.casefold()
    for hint in hints:
        if hint in lowered:
            return hint
    return None


def normalize_application(fields):
    """Turn a flat {question label: answer} Forms row into an application dict.

    Zip the Sheet's header row with the submitted row and pass the result here.
    Grid questions arrive as one column per row, labelled "Question [Row]", and
    become self_reported_skills keyed by the row label. Name, class year and
    major are lifted out by keyword for the Slack header. Every other question
    keeps its text verbatim under "responses", so rewording a form question
    needs no change here.

    Grid answers meaning "not selected" are dropped.
    """
    application = {
        'name': '', 'class_year': '', 'major': '',
        'self_reported_skills': {}, 'responses': {},
    }
    claimed = {}

    for label, value in (fields or {}).items():
        label = " ".join(str(label).split())
        answer = _clean_answer(value)
        if not label:
            continue

        grid = GRID_LABEL.match(label)
        if grid:
            if answer and answer.casefold() not in NEGATIVE_ANSWERS:
                claimed[grid.group('row')] = answer
            continue

        if not answer:
            continue

        for field, hints in (('name', NAME_HINTS),
                             ('class_year', CLASS_YEAR_HINTS),
                             ('major', MAJOR_HINTS)):
            if not application[field] and _matches(label, hints):
                application[field] = answer
                break
        else:
            application['responses'][label] = answer

    application['self_reported_skills'] = claimed
    return application


# --- analysis --------------------------------------------------------------

def _sanitize_text(text):
    """Collapse text to one inert line of Slack mrkdwn.

    Escaping &, < and > stops applicant or model text from becoming a mention,
    link or @channel broadcast; Slack renders the entities literally.
    """
    one_line = " ".join(str(text).split())
    return one_line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _trim(text, max_words):
    """Sanitized text, cut to max_words."""
    words = _sanitize_text(text).split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + " ..."


def _points(values, max_words=MAX_POINT_WORDS):
    """Model strings as trimmed, deduplicated bullet points."""
    points = []
    for value in values or []:
        point = _trim(value, max_words) if isinstance(value, str) else ''
        if point and point not in points:
            points.append(point)
        if len(points) >= MAX_POINTS:
            break
    return points


def clean_analysis(result):
    """Validate a model result into a briefing dict, or None if unusable.

    The strict JSON schema already constrains shape and enums. This pass
    repeats the check without trusting it, trims length, and drops empties.
    """
    if not isinstance(result, dict):
        return None

    headline = _trim(result.get('headline') or '', MAX_HEADLINE_WORDS)
    if not headline:
        # Without a takeaway there is no briefing.
        return None

    skills, seen = [], set()
    for skill in result.get('skills') or []:
        if not isinstance(skill, dict):
            continue
        name = _sanitize_text(skill.get('name') or '')
        level = str(skill.get('evidence_level') or '').strip().casefold()
        summary = _trim(skill.get('summary') or '', MAX_SUMMARY_WORDS)
        key = name.casefold()
        if not name or not summary or level not in SKILL_LEVELS or key in seen:
            continue
        seen.add(key)
        skills.append({'name': name, 'evidence_level': level,
                       'summary': summary})
        if len(skills) >= MAX_SKILLS:
            break

    dependability = None
    raw = result.get('dependability')
    if isinstance(raw, dict):
        level = str(raw.get('evidence_level') or '').strip().casefold()
        summary = _trim(raw.get('summary') or '', MAX_SUMMARY_WORDS)
        if level in DEPENDABILITY_LEVELS and summary:
            dependability = {'evidence_level': level, 'summary': summary}

    return {
        'headline': headline,
        'skills': skills,
        'standout_evidence': _points(result.get('standout_evidence')),
        'limitations_or_uncertainties': _points(
            result.get('limitations_or_uncertainties')),
        'dependability': dependability,
        'questions_to_clarify': _points(result.get('questions_to_clarify')),
    }


def analyze_application(application, api_key=None, client=None):
    """Ask OpenAI what a new RA application demonstrates.

    Takes the dict from normalize_application(), or anything shaped like it,
    and returns a briefing per ANALYSIS_SCHEMA. Returns None on any problem: no
    written answers, no API key, API error, refusal or an unusable result. One
    attempt per application, no retries.

    The applicant's name is never sent; only their answers and, as context,
    class year and major.
    """
    if not isinstance(application, dict):
        return None

    responses = application.get('responses') or {}
    if not any(_clean_answer(v) for v in responses.values()):
        # The grid is self-report with nothing to check it against.
        return None

    if api_key is None:
        api_key = os.environ.get('OPENAI_API_KEY', '')
    if not api_key and client is None:
        return None

    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, timeout=ANALYSIS_TIMEOUT)

        payload = {
            'self_reported_skills': application.get('self_reported_skills') or {},
            'responses': responses,
        }
        for field in ('class_year', 'major'):
            if application.get(field):
                payload[field] = application[field]

        response = client.responses.create(
            model=ANALYSIS_MODEL,
            reasoning={'effort': 'low'},
            # The request carries applicant personal data; do not store it.
            store=False,
            input=[
                {'role': 'system', 'content': ANALYSIS_INSTRUCTIONS},
                {'role': 'user', 'content': json.dumps(payload)},
            ],
            text={'format': {
                'type': 'json_schema',
                'name': 'ra_application_briefing',
                'strict': True,
                'schema': ANALYSIS_SCHEMA,
            }},
        )

        # A refusal carries no output text, so this covers it too.
        output = (getattr(response, 'output_text', '') or '').strip()
        if not output:
            print("No RA briefing: empty model output")
            return None
        result = json.loads(output)
    except Exception as e:
        print(f"No RA briefing: {type(e).__name__}")
        return None

    return clean_analysis(result)


# --- Slack rendering -------------------------------------------------------

def _header(application):
    """The "*New RA application — Jane Doe · 3rd-year PBS*" line.

    Absent fields drop out of it.
    """
    name = _sanitize_text(application.get('name') or '')
    title = f"New RA application — {name}" if name else "New RA application"
    context = " ".join(
        _sanitize_text(application.get(f) or '')
        for f in ('class_year', 'major')
    ).strip()
    if context:
        title += f" \N{MIDDLE DOT} {context}"
    return f"*{title}*"


def _section(heading, lines):
    """One Slack section, or '' when it has no lines."""
    return f"\n*{heading}*\n" + "\n".join(lines) + "\n" if lines else ""


def _link(url):
    """An inert Slack link, or '' for anything but a plain http(s) URL."""
    url = " ".join(str(url or '').split())
    if not url.startswith(('http://', 'https://')):
        return ''
    if any(c in url for c in '<>|'):
        return ''
    return f"\n<{url}|Open application>"


def format_slack(application, analysis, application_url=None):
    """Render one briefing as a compact Slack message.

    All markup comes from here, never from the model. Empty sections are
    dropped. With no analysis, the header alone still announces the
    application.
    """
    application = application if isinstance(application, dict) else {}
    text = _header(application) + "\n"
    if not analysis:
        return text

    text += f"\n*Takeaway:* {analysis.get('headline', '')}\n"

    skills = sorted(
        analysis.get('skills') or [],
        key=lambda s: SKILL_LEVELS.index(s['evidence_level']),
    )
    text += _section('Demonstrated skills', [
        f"{SKILL_MARKERS[s['evidence_level']]} *{s['name']}* — {s['summary']}"
        for s in skills
    ])
    text += _section('Stands out', [
        f"\N{BULLET} {p}" for p in analysis.get('standout_evidence') or []])
    text += _section('Gaps / things to clarify', [
        f"\N{BULLET} {p}" for p in
        (analysis.get('limitations_or_uncertainties') or []) +
        (analysis.get('questions_to_clarify') or [])])

    dependability = analysis.get('dependability')
    if dependability:
        marker = DEPENDABILITY_MARKERS[dependability['evidence_level']]
        text += _section('Dependability',
                         [f"{marker} {dependability['summary']}"])

    return text + _link(application_url)


# --- local inspection ------------------------------------------------------

def _load(path):
    """Read a JSON file as an application, normalizing a flat Forms row."""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('application file is not a JSON object')
    # A normalized application has "responses"; anything else is a flat
    # {question: answer} row from the Sheet.
    return data if 'responses' in data else normalize_application(data)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description="Summarize one RA application from a JSON file and print "
                    "the Slack message."
    )
    parser.add_argument('application', help='JSON file: a normalized '
                                            'application or a flat Forms row')
    parser.add_argument('--url', default=None,
                        help='Link to the application, appended to the message')
    parser.add_argument('--show-input', action='store_true',
                        help='Print the normalized input and exit, no API call')

    args = parser.parse_args()

    # Emoji in the message must not crash a legacy console encoding.
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    application = _load(args.application)
    if args.show_input:
        print(json.dumps(application, indent=2))
        sys.exit(0)

    analysis = analyze_application(application)
    if analysis is None:
        print("No briefing produced")
        sys.exit(1)
    print(format_slack(application, analysis, application_url=args.url))
