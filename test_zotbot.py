#!/usr/bin/env python

"""Regression tests for ZotBot's optional OpenAI enrichment.

    python -m unittest test_zotbot -v

OpenAI is always a fake client, so nothing here hits the network. Every lab
member is invented; no real roster data belongs in this repo.
"""

import json
import unittest
from unittest import mock

import zotbot


# --- fixtures --------------------------------------------------------------

LONG_ABSTRACT = " ".join(f"word{i}" for i in range(1, 131))

ROSTER = [
    {"name": "Example Person", "slack_id": "U0000000001",
     "research": "Fake research area one."},
    {"name": "Another Example", "slack_id": "U0000000002",
     "research": "Fake research area two."},
    {"name": "Third Example", "slack_id": "U0000000003",
     "research": "Fake research area three."},
]


def make_article(abstract=LONG_ABSTRACT, tags=('retina', 'prosthesis')):
    return {
        'version': 1234,
        'data': {
            'key': 'ABCD1234',
            'itemType': 'journalArticle',
            'dateAdded': '2026-09-01T12:00:00Z',
            'title': 'A Test Paper About Nothing',
            'publicationTitle': 'Journal of Tests',
            'date': '2026',
            'DOI': '10.1234/test',
            'abstractNote': abstract,
            'tags': [{'tag': t} for t in tags],
        },
        'meta': {
            'creatorSummary': 'Doe et al.',
            'createdByUser': {'username': 'someone'},
        },
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


def enrich(article, client, roster=ROSTER):
    return zotbot.enrich_article(article, roster, api_key='test-key', client=client)


# --- formatting ------------------------------------------------------------

class FormatArticleTests(unittest.TestCase):

    def test_without_enrichment_output_is_unchanged(self):
        """Requirement 1: the pre-change announcement, byte for byte."""
        article = make_article(abstract="One two three.")
        expected = (
            "<https://doi.org/10.1234/test|*A Test Paper About Nothing*>\n"
            "*Citation:* Doe et al. _Journal of Tests_ 2026\n"
            "*Tags:* retina, prosthesis\n"
            "*Added By:* someone\n"
            "\n*Abstract:*\n```One two three.```"
        )
        self.assertEqual(zotbot.format_article(article), expected)
        self.assertEqual(zotbot.format_article(article, None), expected)

    def test_enrichment_sits_immediately_before_the_abstract(self):
        """Requirement 2."""
        article = make_article(abstract="One two three.")
        text = zotbot.format_article(article, {
            'lab_context': 'Useful comparison for the phosphene work.',
            'mention_ids': ['U0000000001', 'U0000000002'],
        })
        self.assertIn(
            "*Added By:* someone\n"
            "\n*Lab context:* Useful comparison for the phosphene work."
            " <@U0000000001> <@U0000000002>\n"
            "\n*Abstract:*\n```One two three.```",
            text,
        )

    def test_context_without_mentions_still_appears(self):
        """Requirement 3."""
        text = zotbot.format_article(make_article(abstract="Short."), {
            'lab_context': 'Challenges an assumption in our encoding models.',
            'mention_ids': [],
        })
        self.assertIn(
            "\n*Lab context:* Challenges an assumption in our encoding models.\n"
            "\n*Abstract:*",
            text,
        )
        self.assertNotIn("<@", text)

    def test_no_separate_relevant_or_students_line(self):
        text = zotbot.format_article(make_article(), {
            'lab_context': 'Relevant method.', 'mention_ids': ['U0000000001']})
        self.assertNotIn("*Relevant:*", text)
        self.assertNotIn("*Students:*", text)

    def test_abstract_is_still_truncated_to_100_words(self):
        text = zotbot.format_article(make_article())
        self.assertIn("word100 ...```", text)
        self.assertNotIn("word101", text)


# --- validation of model output -------------------------------------------

class EnrichmentValidationTests(unittest.TestCase):

    def test_unknown_and_duplicate_ids_cannot_become_mentions(self):
        """Requirement 4."""
        client = FakeClient({
            'lab_context': 'Direct overlap with the stimulation work.',
            'mention_ids': [
                'U0000000001', 'U0000000001',   # duplicate
                'UEVIL000000',                  # not on the roster
                '<!channel>',                   # not an ID at all
                'U0000000002', 'U0000000003',   # third valid one is over the cap
            ],
        })
        result = enrich(make_article(), client)
        self.assertEqual(result['mention_ids'], ['U0000000001', 'U0000000002'])

        text = zotbot.format_article(make_article(), result)
        self.assertEqual(text.count("<@"), 2)
        self.assertNotIn("UEVIL000000", text)
        self.assertNotIn("<!channel>", text)

    def test_model_prose_cannot_inject_slack_markup(self):
        client = FakeClient({
            'lab_context': "Ping <!channel> and <@UEVIL000000>\nnow",
            'mention_ids': [],
        })
        text = zotbot.format_article(make_article(), enrich(make_article(), client))
        self.assertNotIn("<!channel>", text)
        self.assertNotIn("<@UEVIL000000>", text)
        self.assertIn("&lt;!channel&gt;", text)

    def test_empty_context_discards_mentions(self):
        client = FakeClient({'lab_context': '   ', 'mention_ids': ['U0000000001']})
        self.assertIsNone(enrich(make_article(), client))

    def test_malformed_model_output_is_ignored(self):
        self.assertIsNone(enrich(make_article(), FakeClient(output_text='not json')))
        self.assertIsNone(enrich(make_article(), FakeClient(output_text='')))
        self.assertIsNone(enrich(make_article(), FakeClient(result=['wrong shape'])))

    def test_refusal_falls_back_to_no_enrichment(self):
        # A refused response carries a refusal item and no output text.
        client = FakeClient(output_text='')
        with mock.patch('builtins.print'):
            self.assertIsNone(enrich(make_article(), client))
        self.assertEqual(len(client.calls), 1)


# --- private roster configuration -----------------------------------------

class LoadLabMembersTests(unittest.TestCase):

    def test_valid_roster_is_parsed(self):
        members = zotbot.load_lab_members(json.dumps(ROSTER))
        self.assertEqual([m['slack_id'] for m in members],
                         ['U0000000001', 'U0000000002', 'U0000000003'])

    def test_missing_or_empty_secret_disables_enrichment(self):
        """Requirement 5 (absent configuration)."""
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertEqual(zotbot.load_lab_members(), [])
        self.assertEqual(zotbot.load_lab_members(''), [])
        self.assertEqual(zotbot.load_lab_members('   '), [])
        self.assertEqual(zotbot.load_lab_members('[]'), [])

    def test_malformed_roster_warns_and_disables_enrichment(self):
        """Requirement 5 (malformed configuration)."""
        bad = [
            'not json at all',
            '{"name": "Example Person"}',                 # not a list
            '["just a string"]',                          # entry not an object
            '[{"name": "No Id", "research": "Things."}]',  # no slack_id
            '[{"slack_id": "U0000000001"}]',              # no research
        ]
        for raw in bad:
            with self.subTest(raw=raw[:20]):
                with mock.patch('builtins.print') as printed:
                    self.assertEqual(zotbot.load_lab_members(raw), [])
                printed.assert_called_once_with(
                    "ZotBot enrichment disabled: invalid lab-member configuration")

    def test_empty_roster_makes_no_openai_request(self):
        client = FakeClient({'lab_context': 'x', 'mention_ids': []})
        self.assertIsNone(
            zotbot.enrich_article(make_article(), [], api_key='k', client=client))
        self.assertEqual(client.calls, [])

    def test_missing_api_key_makes_no_openai_request(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(zotbot.enrich_article(make_article(), ROSTER))


# --- failure semantics ----------------------------------------------------

class EnrichmentFailureTests(unittest.TestCase):

    def test_api_error_falls_back_to_the_ordinary_announcement(self):
        """Requirement 6."""
        client = FakeClient(error=RuntimeError('boom'))
        article = make_article(abstract="One two three.")
        self.assertIsNone(enrich(article, client))
        self.assertEqual(zotbot.format_article(article, None),
                         zotbot.format_article(article))
        self.assertNotIn("Lab context", zotbot.format_article(article))

    def test_api_failure_logs_only_key_and_exception_type(self):
        client = FakeClient(error=RuntimeError('secret payload detail'))
        with mock.patch('builtins.print') as printed:
            enrich(make_article(), client)
        logged = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn('ABCD1234', logged)
        self.assertIn('RuntimeError', logged)
        self.assertNotIn('secret payload detail', logged)
        self.assertNotIn('U0000000001', logged)

    def test_paper_without_abstract_makes_no_openai_request(self):
        """Requirement 7."""
        client = FakeClient({'lab_context': 'x', 'mention_ids': []})
        for abstract in ('', '   '):
            self.assertIsNone(enrich(make_article(abstract=abstract), client))
        self.assertEqual(client.calls, [])

    def test_enrichment_failure_does_not_count_as_skipped(self):
        """Requirement 6: a failed enrichment must not lose the paper."""
        article = make_article()
        posted = []
        with mock.patch.object(zotbot, 'retrieve_articles', return_value=[article]), \
             mock.patch.object(zotbot, 'enrich_article',
                               side_effect=RuntimeError('boom')), \
             mock.patch.object(zotbot, 'send_article_to_slack',
                               side_effect=lambda *a, **k: posted.append(k)):
            info = zotbot.main(1, 'C', 'zkey', 'http://hook', mock=True, verbose=False)

        self.assertEqual(info['skipped'], 0)
        self.assertEqual(info['articles_cnt'], 1)
        self.assertEqual(len(posted), 1)
        self.assertIsNone(posted[0]['enrichment'])


# --- what actually goes to OpenAI ----------------------------------------

class OpenAIRequestTests(unittest.TestCase):

    def test_request_carries_full_abstract_while_slack_is_truncated(self):
        """Requirement 8."""
        client = FakeClient({'lab_context': 'Useful method.',
                             'mention_ids': ['U0000000001']})
        article = make_article()
        result = enrich(article, client)

        self.assertEqual(len(client.calls), 1)
        sent = json.loads(client.calls[0]['input'][-1]['content'])
        self.assertEqual(sent['paper']['abstract'], LONG_ABSTRACT)
        self.assertIn('word130', sent['paper']['abstract'])

        text = zotbot.format_article(article, result)
        self.assertNotIn('word101', text)
        self.assertIn('word100 ...```', text)

    def test_request_carries_title_tags_and_roster_but_no_extra_metadata(self):
        client = FakeClient({'lab_context': 'Useful method.', 'mention_ids': []})
        enrich(make_article(), client)

        call = client.calls[0]
        sent = json.loads(call['input'][-1]['content'])
        self.assertEqual(sent['paper']['title'], 'A Test Paper About Nothing')
        self.assertEqual(sent['paper']['tags'], ['retina', 'prosthesis'])
        self.assertEqual(sent['lab_members'], ROSTER)

        blob = json.dumps(call, default=str)
        self.assertNotIn('someone', blob)        # submitter
        self.assertNotIn('10.1234/test', blob)   # DOI
        self.assertNotIn('ABCD1234', blob)       # Zotero key

    def test_request_uses_the_fixed_model_settings_and_no_tools(self):
        client = FakeClient({'lab_context': 'Useful method.', 'mention_ids': []})
        enrich(make_article(), client)

        call = client.calls[0]
        self.assertEqual(call['model'], 'gpt-5.6')
        self.assertEqual(call['reasoning'], {'effort': 'low'})
        self.assertEqual(call['text']['format']['type'], 'json_schema')
        self.assertTrue(call['text']['format']['strict'])
        self.assertNotIn('tools', call)

    def test_tags_are_omitted_when_absent(self):
        client = FakeClient({'lab_context': 'Useful method.', 'mention_ids': []})
        enrich(make_article(tags=()), client)
        sent = json.loads(client.calls[0]['input'][-1]['content'])
        self.assertNotIn('tags', sent['paper'])

    def test_one_attempt_per_paper(self):
        client = FakeClient(error=RuntimeError('boom'))
        enrich(make_article(), client)
        self.assertEqual(len(client.calls), 1)


if __name__ == '__main__':
    unittest.main()
