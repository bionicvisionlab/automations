#!/usr/bin/env python

"""Regression tests for the RA application briefing.

    python -m unittest test_ra_applicant -v

OpenAI is always a fake client, so nothing here hits the network. Both
applicants are invented; no real application data belongs in this repo.
"""

import json
import unittest
from unittest import mock

import ra_applicant


# --- fixtures --------------------------------------------------------------

# 1. Real human-subjects and psychophysics experience, with tools,
#    difficulties and scale.
EXPERIENCED = {
    'name': 'Jane Doe',
    'class_year': '3rd-year',
    'major': 'PBS',
    'self_reported_skills': {
        'Human subjects research': 'Yes',
        'Psychophysics software': 'Yes',
        'Programming': 'Yes',
        'Eye tracking': 'Yes',
        'ML/AI models': 'Yes',
    },
    'responses': {
        'Describe one project or responsibility that best demonstrates what you '
        'could contribute right now.':
            "In the Example Perception Lab I modified a PsychoPy contrast "
            "detection task, synchronized it with an EyeLink 1000, and "
            "independently scheduled and ran about 40 participants over two "
            "quarters. The hard part was a 12 ms trigger lag between the "
            "display and the tracker, which I tracked down with a photodiode.",
        'Describe an example that shows how dependable you are.':
            "I held the same Tuesday and Thursday 8am testing slot for two "
            "quarters without missing a session, and wrote a handoff document "
            "when I went on exchange so the study kept running.",
        'What is your availability next quarter?':
            "About 12 hours per week; I graduate in June.",
    },
}

# 2. Enthusiastic, with coursework, a club and a tutorial behind it.
COURSEWORK = {
    'name': 'Alex Roe',
    'class_year': '1st-year',
    'major': 'CS',
    'self_reported_skills': {
        'Programming': 'Yes',
        'ML/AI models': 'Yes',
        'EEG/BCI': 'Yes',
        'Blind/clinical populations': 'Yes',
    },
    'responses': {
        'Describe one project or responsibility that best demonstrates what you '
        'could contribute right now.':
            "I took an intro programming course and built the final project, a "
            "number guessing game. I also attended BCI club meetings this year "
            "and followed an online tutorial on neural networks.",
        'Describe an example that shows how dependable you are.':
            "I am very passionate about vision research and always try my best.",
        'What is your availability next quarter?':
            "Whenever you need me.",
    },
}

# A plausible model result for the experienced applicant. These fixtures
# protect the shape; no test asserts the prose.
BRIEFING = {
    'headline': "Concrete psychophysics and participant-running experience; "
                "programming evidence is thinner than the grid suggests.",
    'skills': [
        {'name': 'Human subjects research', 'evidence_level': 'substantial',
         'summary': "Independently scheduled and ran about 40 participants "
                    "over two quarters."},
        {'name': 'Psychophysics software', 'evidence_level': 'substantial',
         'summary': "Modified and debugged a PsychoPy contrast detection task."},
        {'name': 'Eye tracking', 'evidence_level': 'some',
         'summary': "Synchronized an EyeLink 1000; unclear whether they "
                    "configured it from scratch."},
        {'name': 'ML/AI models', 'evidence_level': 'unsupported',
         'summary': "Selected in the grid but absent from the written answers."},
    ],
    'standout_evidence': [
        "Diagnosed a 12 ms display-to-tracker lag with a photodiode.",
    ],
    'limitations_or_uncertainties': [
        "Graduates in June, so the time available is about two quarters.",
    ],
    'dependability': {
        'evidence_level': 'substantial',
        'summary': "Held the same 8am slot for two quarters and wrote a "
                   "handoff document before leaving.",
    },
    'questions_to_clarify': [
        "Did they write the synchronization code or use an existing script?",
    ],
}


class FakeResponse:
    def __init__(self, output_text):
        self.output_text = output_text


class FakeClient:
    """Records Responses API calls and replays a canned result."""

    def __init__(self, result=None, error=None, output_text=None):
        self.calls = []
        self.responses = self
        self._result = result
        self._error = error
        self._output_text = output_text

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        if self._output_text is not None:
            return FakeResponse(self._output_text)
        return FakeResponse(json.dumps(self._result))


def analyze(application, client):
    return ra_applicant.analyze_application(
        application, api_key='test-key', client=client)


def sent_payload(client, call=0):
    return json.loads(client.calls[call]['input'][-1]['content'])


# --- what actually goes to OpenAI -----------------------------------------

class OpenAIRequestTests(unittest.TestCase):

    def test_request_is_not_stored(self):
        """Applicant responses are personal data."""
        client = FakeClient(BRIEFING)
        analyze(EXPERIENCED, client)
        self.assertIs(client.calls[0]['store'], False)

    def test_request_uses_the_fixed_model_settings_and_no_tools(self):
        client = FakeClient(BRIEFING)
        analyze(EXPERIENCED, client)

        call = client.calls[0]
        self.assertEqual(call['model'], 'gpt-5.6')
        self.assertEqual(call['reasoning'], {'effort': 'low'})
        self.assertNotIn('tools', call)

    def test_request_carries_the_written_answers_and_the_claimed_grid(self):
        client = FakeClient(BRIEFING)
        analyze(EXPERIENCED, client)

        sent = sent_payload(client)
        self.assertEqual(sent['responses'], EXPERIENCED['responses'])
        self.assertEqual(sent['self_reported_skills'],
                         EXPERIENCED['self_reported_skills'])
        # Question labels travel verbatim.
        self.assertIn('What is your availability next quarter?',
                      sent['responses'])
        self.assertIn('photodiode', json.dumps(sent))

    def test_class_year_and_major_are_context_but_the_name_is_not_sent(self):
        client = FakeClient(BRIEFING)
        analyze(EXPERIENCED, client)

        sent = sent_payload(client)
        self.assertEqual(sent['class_year'], '3rd-year')
        self.assertEqual(sent['major'], 'PBS')
        self.assertNotIn('name', sent)
        self.assertNotIn('Jane Doe', json.dumps(client.calls[0], default=str))

    def test_context_fields_are_omitted_when_absent(self):
        client = FakeClient(BRIEFING)
        analyze({'responses': {'Q': 'A real answer.'}}, client)

        sent = sent_payload(client)
        self.assertNotIn('class_year', sent)
        self.assertNotIn('major', sent)

    def test_strict_schema_carries_the_intended_evidence_levels(self):
        client = FakeClient(BRIEFING)
        analyze(EXPERIENCED, client)

        fmt = client.calls[0]['text']['format']
        self.assertEqual(fmt['type'], 'json_schema')
        self.assertTrue(fmt['strict'])

        schema = fmt['schema']
        skill = schema['properties']['skills']['items']
        self.assertEqual(skill['properties']['evidence_level']['enum'],
                         ['substantial', 'some', 'exposure', 'unsupported'])
        self.assertEqual(
            schema['properties']['dependability']['properties']
                  ['evidence_level']['enum'],
            ['substantial', 'some', 'limited', 'none'])

        # Strict structured output needs closed objects with every
        # property required.
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(sorted(schema['required']),
                         sorted(schema['properties']))
        self.assertFalse(skill['additionalProperties'])
        self.assertEqual(sorted(skill['required']), sorted(skill['properties']))

    def test_no_skill_has_to_be_reported(self):
        """The grid has nine rows; the briefing need not fill them."""
        schema = ra_applicant.ANALYSIS_SCHEMA['properties']['skills']
        self.assertNotIn('minItems', schema)

    def test_one_attempt_per_application(self):
        client = FakeClient(error=RuntimeError('boom'))
        self.assertIsNone(analyze(EXPERIENCED, client))
        self.assertEqual(len(client.calls), 1)


# --- no scoring, ranking or hiring advice ---------------------------------

class NoRecommendationTests(unittest.TestCase):

    BANNED = ('score', 'rating', 'rank', 'recommend', 'hire', 'reject',
              'interview', 'fit', 'overall')

    def test_schema_introduces_no_score_or_recommendation_field(self):
        blob = json.dumps(ra_applicant.ANALYSIS_SCHEMA).casefold()
        for word in self.BANNED:
            with self.subTest(word=word):
                self.assertNotIn(f'"{word}', blob)
                self.assertNotIn(f'_{word}', blob)

    def test_evidence_levels_are_not_quality_labels(self):
        levels = set(ra_applicant.SKILL_LEVELS + ra_applicant.DEPENDABILITY_LEVELS)
        self.assertFalse(levels & {'excellent', 'good', 'poor', 'strong',
                                   'weak', 'high', 'low'})

    def test_instructions_forbid_scoring_and_hiring_advice(self):
        text = " ".join(ra_applicant.ANALYSIS_INSTRUCTIONS.split()).casefold()
        for phrase in ('do not score, rate, rank', 'do not recommend hiring',
                       'demographic'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_slack_message_introduces_no_verdict_of_its_own(self):
        text = ra_applicant.format_slack(EXPERIENCED,
                                         ra_applicant.clean_analysis(BRIEFING))
        # The model's prose may use such words; our headings may not.
        headings = [line for line in text.splitlines()
                    if line.startswith('*') and line.endswith('*')]
        for line in headings:
            for word in self.BANNED:
                with self.subTest(line=line, word=word):
                    self.assertNotIn(word, line.casefold())


# --- parsing and defensive validation --------------------------------------

class CleanAnalysisTests(unittest.TestCase):

    def test_a_valid_result_is_parsed(self):
        client = FakeClient(BRIEFING)
        analysis = analyze(EXPERIENCED, client)

        self.assertEqual(analysis['headline'], BRIEFING['headline'])
        self.assertEqual([s['name'] for s in analysis['skills']],
                         [s['name'] for s in BRIEFING['skills']])
        self.assertEqual(analysis['dependability']['evidence_level'],
                         'substantial')
        self.assertEqual(len(analysis['questions_to_clarify']), 1)

    def test_unknown_evidence_levels_and_empty_skills_are_dropped(self):
        analysis = ra_applicant.clean_analysis({
            'headline': 'A takeaway.',
            'skills': [
                {'name': 'Programming', 'evidence_level': 'excellent',
                 'summary': 'Not an allowed level.'},
                {'name': '', 'evidence_level': 'some', 'summary': 'No name.'},
                {'name': 'Eye tracking', 'evidence_level': 'some',
                 'summary': ''},
                {'name': 'EEG/BCI', 'evidence_level': 'exposure',
                 'summary': 'Attended club meetings.'},
                'not an object',
            ],
            'dependability': {'evidence_level': 'unknown', 'summary': 'x'},
        })
        self.assertEqual([s['name'] for s in analysis['skills']], ['EEG/BCI'])
        self.assertIsNone(analysis['dependability'])
        self.assertEqual(analysis['standout_evidence'], [])

    def test_duplicate_skills_and_points_appear_once(self):
        analysis = ra_applicant.clean_analysis({
            'headline': 'A takeaway.',
            'skills': [
                {'name': 'Programming', 'evidence_level': 'some', 'summary': 'a'},
                {'name': 'programming', 'evidence_level': 'exposure',
                 'summary': 'b'},
            ],
            'standout_evidence': ['Same point.', 'Same point.'],
        })
        self.assertEqual(len(analysis['skills']), 1)
        self.assertEqual(analysis['standout_evidence'], ['Same point.'])

    def test_model_prose_cannot_inject_slack_markup(self):
        analysis = ra_applicant.clean_analysis({
            'headline': 'Ping <!channel> & <@U123> now.',
            'skills': [{'name': '<@U999>', 'evidence_level': 'some',
                        'summary': 'See <http://evil|here>.'}],
        })
        text = ra_applicant.format_slack(EXPERIENCED, analysis)
        self.assertNotIn('<!channel>', text)
        self.assertNotIn('<@U123>', text)
        self.assertNotIn('<@U999>', text)
        self.assertIn('&lt;!channel&gt;', text)

    def test_a_result_without_a_headline_is_unusable(self):
        self.assertIsNone(ra_applicant.clean_analysis(
            {'headline': '   ', 'skills': BRIEFING['skills']}))

    def test_a_non_dict_result_is_unusable(self):
        for result in (None, [], 'text', 7):
            with self.subTest(result=result):
                self.assertIsNone(ra_applicant.clean_analysis(result))

    def test_long_prose_is_trimmed(self):
        long_text = " ".join(f"word{i}" for i in range(1, 201))
        analysis = ra_applicant.clean_analysis({
            'headline': long_text,
            'skills': [{'name': 'Programming', 'evidence_level': 'some',
                        'summary': long_text}],
            'standout_evidence': [long_text],
        })
        self.assertLessEqual(len(analysis['headline'].split()),
                             ra_applicant.MAX_HEADLINE_WORDS + 1)
        self.assertLessEqual(len(analysis['skills'][0]['summary'].split()),
                             ra_applicant.MAX_SUMMARY_WORDS + 1)
        self.assertLessEqual(len(analysis['standout_evidence'][0].split()),
                             ra_applicant.MAX_POINT_WORDS + 1)

    def test_too_many_points_are_capped(self):
        analysis = ra_applicant.clean_analysis({
            'headline': 'A takeaway.',
            'standout_evidence': [f"Point {i}." for i in range(20)],
            'questions_to_clarify': [f"Question {i}?" for i in range(20)],
        })
        self.assertEqual(len(analysis['standout_evidence']),
                         ra_applicant.MAX_POINTS)
        self.assertEqual(len(analysis['questions_to_clarify']),
                         ra_applicant.MAX_POINTS)


# --- failure semantics -----------------------------------------------------

class FailureTests(unittest.TestCase):

    def test_api_error_produces_no_briefing(self):
        self.assertIsNone(analyze(EXPERIENCED,
                                  FakeClient(error=RuntimeError('boom'))))

    def test_refusal_or_empty_output_produces_no_briefing(self):
        for output in ('', '   '):
            with self.subTest(output=output):
                self.assertIsNone(
                    analyze(EXPERIENCED, FakeClient(output_text=output)))

    def test_malformed_model_output_produces_no_briefing(self):
        self.assertIsNone(
            analyze(EXPERIENCED, FakeClient(output_text='{not json')))

    def test_failures_log_no_applicant_data(self):
        with mock.patch('builtins.print') as printed:
            analyze(EXPERIENCED, FakeClient(error=RuntimeError('Jane Doe')))
        logged = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn('RuntimeError', logged)
        self.assertNotIn('Jane Doe', logged)
        self.assertNotIn('photodiode', logged)

    def test_an_application_without_written_answers_makes_no_request(self):
        client = FakeClient(BRIEFING)
        application = dict(EXPERIENCED, responses={'Anything?': '   '})
        self.assertIsNone(analyze(application, client))
        self.assertEqual(client.calls, [])

    def test_missing_api_key_makes_no_request(self):
        self.assertIsNone(ra_applicant.analyze_application(
            EXPERIENCED, api_key=''))

    def test_a_non_dict_application_makes_no_request(self):
        client = FakeClient(BRIEFING)
        self.assertIsNone(analyze(None, client))
        self.assertEqual(client.calls, [])


# --- Slack rendering -------------------------------------------------------

class FormatSlackTests(unittest.TestCase):

    def briefing(self, **overrides):
        return ra_applicant.clean_analysis(dict(BRIEFING, **overrides))

    def test_header_carries_name_class_year_and_major(self):
        text = ra_applicant.format_slack(EXPERIENCED, self.briefing())
        self.assertTrue(text.startswith(
            "*New RA application — Jane Doe · 3rd-year PBS*\n"))

    def test_header_degrades_when_context_is_missing(self):
        text = ra_applicant.format_slack({'name': 'Jane Doe'}, self.briefing())
        self.assertTrue(text.startswith("*New RA application — Jane Doe*\n"))
        text = ra_applicant.format_slack({}, self.briefing())
        self.assertTrue(text.startswith("*New RA application*\n"))

    def test_demonstrated_and_unsupported_claims_are_visually_distinct(self):
        text = ra_applicant.format_slack(EXPERIENCED, self.briefing())
        substantial = [l for l in text.splitlines()
                       if 'Human subjects research' in l][0]
        unsupported = [l for l in text.splitlines() if 'ML/AI models' in l][0]

        self.assertTrue(substantial.startswith('\N{LARGE GREEN CIRCLE}'))
        self.assertTrue(unsupported.startswith('\N{MEDIUM WHITE CIRCLE}'))
        self.assertNotEqual(substantial[0], unsupported[0])
        self.assertIn('\N{LARGE YELLOW CIRCLE} *Eye tracking*', text)

    def test_skills_are_ordered_from_best_evidenced_to_unsupported(self):
        text = ra_applicant.format_slack(EXPERIENCED, self.briefing(skills=[
            {'name': 'ML/AI models', 'evidence_level': 'unsupported',
             'summary': 'Nothing in the prose.'},
            {'name': 'EEG/BCI', 'evidence_level': 'exposure',
             'summary': 'Club meetings.'},
            {'name': 'Human subjects research', 'evidence_level': 'substantial',
             'summary': 'Ran 40 participants.'},
        ]))
        order = [text.index(n) for n in
                 ('Human subjects research', 'EEG/BCI', 'ML/AI models')]
        self.assertEqual(order, sorted(order))

    def test_empty_sections_are_omitted(self):
        text = ra_applicant.format_slack(COURSEWORK, self.briefing(
            standout_evidence=[], limitations_or_uncertainties=[],
            questions_to_clarify=[], skills=[], dependability=None))
        for heading in ('Demonstrated skills', 'Stands out',
                        'Gaps / things to clarify', 'Dependability'):
            with self.subTest(heading=heading):
                self.assertNotIn(heading, text)
        self.assertIn('*Takeaway:*', text)

    def test_gaps_section_merges_limitations_and_questions(self):
        text = ra_applicant.format_slack(EXPERIENCED, self.briefing())
        self.assertEqual(text.count('*Gaps / things to clarify*'), 1)
        self.assertIn('Graduates in June', text)
        self.assertIn('Did they write the synchronization code', text)

    def test_no_analysis_still_announces_the_application(self):
        text = ra_applicant.format_slack(EXPERIENCED, None)
        self.assertEqual(text, "*New RA application — Jane Doe · "
                               "3rd-year PBS*\n")

    def test_application_url_appears_when_supplied(self):
        text = ra_applicant.format_slack(
            EXPERIENCED, self.briefing(),
            application_url='https://docs.google.com/spreadsheets/d/x#gid=0')
        self.assertIn(
            "<https://docs.google.com/spreadsheets/d/x#gid=0|Open application>",
            text)

    def test_no_url_means_no_link(self):
        text = ra_applicant.format_slack(EXPERIENCED, self.briefing())
        self.assertNotIn('Open application', text)

    def test_a_non_http_or_markup_bearing_url_is_dropped(self):
        for url in ('javascript:alert(1)', 'https://x|<@U123>', 'not a url'):
            with self.subTest(url=url):
                text = ra_applicant.format_slack(EXPERIENCED, self.briefing(),
                                                 application_url=url)
                self.assertNotIn('Open application', text)

    def test_the_message_stays_phone_sized(self):
        """A maximal briefing still has to fit on a phone."""
        maximal = ra_applicant.clean_analysis({
            'headline': "A " + " ".join(["word"] * 60),
            'skills': [{'name': f"Skill {i}", 'evidence_level': 'substantial',
                        'summary': " ".join(["word"] * 60)} for i in range(15)],
            'standout_evidence': [" ".join(["word"] * 60)] * 10,
            'limitations_or_uncertainties': [" ".join(["word"] * 60)] * 10,
            'questions_to_clarify': [" ".join(["word"] * 60)] * 10,
            'dependability': {'evidence_level': 'some',
                              'summary': " ".join(["word"] * 60)},
        })
        text = ra_applicant.format_slack(EXPERIENCED, maximal)
        self.assertLessEqual(len(text), 2500)
        self.assertLessEqual(len(text.splitlines()), 40)

    def test_a_realistic_briefing_is_short(self):
        text = ra_applicant.format_slack(EXPERIENCED, self.briefing())
        self.assertLessEqual(len(text), 1200)


# --- Google Forms normalization -------------------------------------------

class NormalizeApplicationTests(unittest.TestCase):

    ROW = {
        'Timestamp': '2026-09-19 10:04:11',
        'Full name': '  Jane   Doe ',
        'Class year': '3rd-year',
        'Major': 'PBS',
        'Which do you have experience with? [Human subjects research]': 'Yes',
        'Which do you have experience with? [Eye tracking]': 'Yes',
        'Which do you have experience with? [EEG/BCI]': 'No',
        'Which do you have experience with? [Unity/VR/AR]': '',
        'Describe one project or responsibility.': 'I ran 40 participants.',
        'Describe an example that shows how dependable you are.': 'I showed up.',
        'Anything else?': '',
    }

    def test_grid_columns_become_self_reported_skills(self):
        app = ra_applicant.normalize_application(self.ROW)
        self.assertEqual(app['self_reported_skills'],
                         {'Human subjects research': 'Yes',
                          'Eye tracking': 'Yes'})

    def test_unselected_and_blank_grid_rows_are_not_claims(self):
        app = ra_applicant.normalize_application(self.ROW)
        self.assertNotIn('EEG/BCI', app['self_reported_skills'])
        self.assertNotIn('Unity/VR/AR', app['self_reported_skills'])

    def test_name_class_year_and_major_are_lifted_out(self):
        app = ra_applicant.normalize_application(self.ROW)
        self.assertEqual(app['name'], 'Jane Doe')
        self.assertEqual(app['class_year'], '3rd-year')
        self.assertEqual(app['major'], 'PBS')
        for field in ('Full name', 'Class year', 'Major'):
            self.assertNotIn(field, app['responses'])

    def test_other_questions_keep_their_labels_verbatim(self):
        app = ra_applicant.normalize_application(self.ROW)
        self.assertEqual(app['responses'], {
            'Timestamp': '2026-09-19 10:04:11',
            'Describe one project or responsibility.': 'I ran 40 participants.',
            'Describe an example that shows how dependable you are.':
                'I showed up.',
        })

    def test_checkbox_lists_become_text(self):
        app = ra_applicant.normalize_application(
            {'Q [Programming]': ['Python', 'MATLAB'], 'Prose': 'Some answer.'})
        self.assertEqual(app['self_reported_skills'],
                         {'Programming': 'Python, MATLAB'})

    def test_an_empty_row_normalizes_without_error(self):
        app = ra_applicant.normalize_application({})
        self.assertEqual(app['responses'], {})
        self.assertEqual(app['self_reported_skills'], {})
        self.assertEqual(app['name'], '')

    def test_a_normalized_row_can_be_analyzed_directly(self):
        client = FakeClient(BRIEFING)
        analyze(ra_applicant.normalize_application(self.ROW), client)
        sent = sent_payload(client)
        self.assertIn('I ran 40 participants.', sent['responses'].values())
        self.assertEqual(sent['self_reported_skills']['Eye tracking'], 'Yes')


# --- the two fixture applications end to end -------------------------------

class FixtureApplicationTests(unittest.TestCase):

    def test_both_fixtures_reach_the_model_with_their_own_evidence(self):
        for application, marker in ((EXPERIENCED, 'photodiode'),
                                    (COURSEWORK, 'BCI club meetings')):
            with self.subTest(applicant=application['name']):
                client = FakeClient(BRIEFING)
                analyze(application, client)
                self.assertIn(marker, json.dumps(sent_payload(client)))

    def test_a_coursework_briefing_renders_its_own_calibration(self):
        """A ticked grid row with no prose behind it."""
        analysis = ra_applicant.clean_analysis({
            'headline': "Enthusiastic first-year whose evidence is coursework "
                        "and club attendance rather than research.",
            'skills': [
                {'name': 'Programming', 'evidence_level': 'exposure',
                 'summary': "One intro course project; no research code."},
                {'name': 'EEG/BCI', 'evidence_level': 'exposure',
                 'summary': "Attended BCI club meetings."},
                {'name': 'Blind/clinical populations',
                 'evidence_level': 'unsupported',
                 'summary': "Ticked in the grid, absent from the answers."},
            ],
            'standout_evidence': [],
            'limitations_or_uncertainties': [
                "Availability is stated only as 'whenever you need me'.",
            ],
            'dependability': {'evidence_level': 'none',
                              'summary': "The answer describes enthusiasm, "
                                         "not a kept commitment."},
            'questions_to_clarify': ["How many hours per week are realistic?"],
        })
        text = ra_applicant.format_slack(COURSEWORK, analysis)

        self.assertIn("*New RA application — Alex Roe · 1st-year CS*",
                      text)
        self.assertIn('\N{LARGE ORANGE CIRCLE} *Programming*', text)
        self.assertIn('\N{MEDIUM WHITE CIRCLE} *Blind/clinical populations*',
                      text)
        self.assertNotIn('Stands out', text)
        self.assertIn('*Dependability*', text)
        self.assertIn('\N{MEDIUM WHITE CIRCLE} The answer describes', text)


if __name__ == '__main__':
    unittest.main()
