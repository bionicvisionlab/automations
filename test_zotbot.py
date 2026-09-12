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

RAW_ROSTER = [
    {"name": "Example Person", "slack_id": "U0000000001",
     "research": "Fake research area one."},
    {"name": "Another Example", "slack_id": "U0000000002",
     "research": "Fake research area two."},
    {"name": "Third Example", "slack_id": "U0000000003",
     "research": "Fake research area three."},
]

# What load_lab_members() makes of the three-field entries above.
ROSTER = zotbot.load_lab_members(json.dumps(RAW_ROSTER))

PI_ENTRY = {"name": "Example Chief", "slack_id": "",
            "research": "Fake research area four.",
            "role": "pi", "notify": False}

# Roster with a PI who never gets mentioned, as the real secret will look.
ROSTER_WITH_PI = zotbot.load_lab_members(json.dumps(RAW_ROSTER + [PI_ENTRY]))


def author(first, last, creator_type='author'):
    return {'creatorType': creator_type, 'firstName': first, 'lastName': last}


def make_article(abstract=LONG_ABSTRACT, tags=('retina', 'prosthesis'),
                 creators=None):
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
            'creators': list(creators) if creators is not None else [
                author('Jane', 'Doe'), author('John', 'Roe'),
            ],
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


def sent_payload(client, call=0):
    return json.loads(client.calls[call]['input'][-1]['content'])


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
        # The empty-output case is covered by the refusal test below.
        self.assertIsNone(enrich(make_article(), FakeClient(output_text='not json')))
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
        members = zotbot.load_lab_members(json.dumps(RAW_ROSTER))
        self.assertEqual([m['slack_id'] for m in members],
                         ['U0000000001', 'U0000000002', 'U0000000003'])

    def test_three_field_entries_default_to_notifyable_members(self):
        """Requirement 1: the existing secret keeps working unmodified."""
        for member in zotbot.load_lab_members(json.dumps(RAW_ROSTER)):
            self.assertEqual(member['role'], 'member')
            self.assertIs(member['notify'], True)

    def test_pi_entry_is_valid_without_a_slack_id(self):
        """Requirement 2."""
        members = zotbot.load_lab_members(json.dumps([PI_ENTRY]))
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]['role'], 'pi')
        self.assertIs(members[0]['notify'], False)
        self.assertEqual(members[0]['slack_id'], '')

    def test_notifyable_member_without_slack_id_is_invalid(self):
        """Requirement 3."""
        for entry in ({"name": "No Id", "research": "Things."},
                      {"name": "No Id", "research": "Things.", "notify": True},
                      {"name": "No Id", "research": "Things.", "slack_id": "  "}):
            with self.subTest(entry=entry):
                with mock.patch('builtins.print'):
                    self.assertEqual(
                        zotbot.load_lab_members(json.dumps([entry])), [])

    def test_name_is_now_required(self):
        """Names drive author matching, so a nameless entry is malformed."""
        raw = json.dumps([{"slack_id": "U0000000001", "research": "Things."}])
        with mock.patch('builtins.print'):
            self.assertEqual(zotbot.load_lab_members(raw), [])

    def test_unknown_role_or_non_boolean_notify_is_rejected_whole(self):
        bad = [
            [{"name": "X", "slack_id": "U1", "research": "R", "role": "chief"}],
            [{"name": "X", "slack_id": "U1", "research": "R", "notify": "yes"}],
        ]
        for entry in bad:
            with self.subTest(entry=entry):
                with mock.patch('builtins.print') as printed:
                    self.assertEqual(
                        zotbot.load_lab_members(json.dumps(entry)), [])
                printed.assert_called_once_with(
                    "ZotBot enrichment disabled: invalid lab-member configuration")

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
        self.assertNotIn('tools', call)

        # The request carries the private roster, so it must not be stored.
        self.assertIs(call['store'], False)

        fmt = call['text']['format']
        self.assertEqual(fmt['type'], 'json_schema')
        self.assertTrue(fmt['strict'])
        self.assertEqual(fmt['schema']['properties']['mention_ids']['maxItems'], 2)

    def test_tags_are_omitted_when_absent(self):
        client = FakeClient({'lab_context': 'Useful method.', 'mention_ids': []})
        enrich(make_article(tags=()), client)
        sent = json.loads(client.calls[0]['input'][-1]['content'])
        self.assertNotIn('tags', sent['paper'])

    def test_one_attempt_per_paper(self):
        client = FakeClient(error=RuntimeError('boom'))
        enrich(make_article(), client)
        self.assertEqual(len(client.calls), 1)


# --- author extraction and matching ---------------------------------------

class AuthorExtractionTests(unittest.TestCase):

    def test_only_authors_count_and_order_is_preserved(self):
        """Requirement 4."""
        data = {'creators': [
            author('Ada', 'First'),
            author('Bob', 'Editor', creator_type='editor'),
            author('Cleo', 'Second'),
            author('Dan', 'Translator', creator_type='translator'),
            author('Eve', 'Third', creator_type='contributor'),
        ]}
        self.assertEqual(zotbot.extract_authors(data),
                         ['Ada First', 'Cleo Second'])

    def test_single_field_and_missing_names(self):
        data = {'creators': [
            {'creatorType': 'author', 'name': 'Example  Consortium'},
            {'creatorType': 'author', 'lastName': 'Mononym'},
            {'creatorType': 'author', 'firstName': 'Given'},
            {'creatorType': 'author'},          # nothing usable
            'not a dict',
        ]}
        self.assertEqual(zotbot.extract_authors(data),
                         ['Example Consortium', 'Mononym', 'Given'])

    def test_missing_creators_is_not_an_error(self):
        self.assertEqual(zotbot.extract_authors({}), [])
        self.assertEqual(zotbot.extract_authors({'creators': None}), [])

    def test_matching_is_exact_on_normalized_full_names(self):
        """Requirement 5."""
        authors = ['Someone Else', '  example   person ', 'ANOTHER EXAMPLE']
        matched = zotbot.match_lab_authors(authors, ROSTER)
        self.assertEqual([(m['name'], m['author_position']) for m in matched],
                         [('Example Person', 2), ('Another Example', 3)])

    def test_no_surname_only_or_fuzzy_matching(self):
        """Requirement 6."""
        authors = [
            'Person',                # surname only
            'Example',               # given name only
            'Example P. Person',     # middle initial
            'Examples Person',       # near miss
            'Example Personson',     # prefix of a roster name
        ]
        self.assertEqual(zotbot.match_lab_authors(authors, ROSTER), [])

    def test_matched_author_carries_roster_role_and_notify(self):
        matched = zotbot.match_lab_authors(['Example Chief'], ROSTER_WITH_PI)
        self.assertEqual(matched, [{
            'name': 'Example Chief', 'author_position': 1,
            'role': 'pi', 'notify': False, 'slack_id': '',
        }])


class PaperModeTests(unittest.TestCase):

    def context(self, creators, roster=ROSTER_WITH_PI):
        return zotbot.author_context(
            make_article(creators=creators)['data'], roster)

    def test_no_lab_authors_is_external(self):
        """Requirement 7."""
        context = self.context([author('Jane', 'Doe'), author('John', 'Roe')])
        self.assertEqual(context['paper_mode'], 'external')
        self.assertEqual(context['lab_authors'], [])
        self.assertEqual(context['authors'], ['Jane Doe', 'John Roe'])

    def test_member_and_pi_authors_is_a_lab_paper(self):
        """Requirement 8."""
        context = self.context([
            author('Example', 'Person'),
            author('Jane', 'Doe'),
            author('Example', 'Chief'),
        ])
        self.assertEqual(context['paper_mode'], 'lab')
        self.assertEqual([(a['name'], a['author_position'], a['role'])
                          for a in context['lab_authors']],
                         [('Example Person', 1, 'member'),
                          ('Example Chief', 3, 'pi')])

    def test_member_author_without_the_pi_is_a_lab_paper(self):
        """Requirement 9."""
        context = self.context([author('Jane', 'Doe'),
                                author('Another', 'Example')])
        self.assertEqual(context['paper_mode'], 'lab')
        self.assertEqual([a['author_position'] for a in context['lab_authors']],
                         [2])

    def test_several_member_authors_stay_a_lab_paper(self):
        context = self.context([author('Example', 'Person'),
                                author('Third', 'Example')])
        self.assertEqual(context['paper_mode'], 'lab')
        self.assertEqual(len(context['lab_authors']), 2)

    def test_pi_as_the_only_lab_author_is_a_collaboration(self):
        """Requirement 10."""
        context = self.context([author('Jane', 'Doe'),
                                author('Example', 'Chief')])
        self.assertEqual(context['paper_mode'], 'collaboration')
        self.assertEqual([a['role'] for a in context['lab_authors']], ['pi'])

    def test_an_editing_lab_member_does_not_make_the_paper_ours(self):
        context = self.context([
            author('Jane', 'Doe'),
            author('Example', 'Person', creator_type='editor'),
        ])
        self.assertEqual(context['paper_mode'], 'external')

    def test_a_roster_without_a_pi_still_works(self):
        context = self.context([author('Example', 'Chief')], roster=ROSTER)
        self.assertEqual(context['paper_mode'], 'external')


# --- mode-dependent mention routing ---------------------------------------

class ModeMentionTests(unittest.TestCase):

    def lab_paper(self):
        # First author is a lab member; the PI is also on the paper.
        return make_article(creators=[
            author('Example', 'Person'),
            author('Jane', 'Doe'),
            author('Example', 'Chief'),
        ])

    def test_lab_paper_discards_ids_of_non_author_members(self):
        """Requirement 11."""
        client = FakeClient({
            'lab_context': 'Example Person and colleagues show something. Congrats!',
            'mention_ids': ['U0000000002', 'U0000000003', 'U0000000001'],
        })
        result = enrich(self.lab_paper(), client, roster=ROSTER_WITH_PI)
        self.assertEqual(result['mention_ids'], ['U0000000001'])

    def test_lab_paper_keeps_a_notifyable_lab_author(self):
        """Requirement 12."""
        client = FakeClient({
            'lab_context': 'Example Person and colleagues report a result. Congrats!',
            'mention_ids': ['U0000000001'],
        })
        result = enrich(self.lab_paper(), client, roster=ROSTER_WITH_PI)
        self.assertEqual(result['mention_ids'], ['U0000000001'])
        text = zotbot.format_article(self.lab_paper(), result)
        self.assertIn("*Lab context:* Example Person and colleagues report "
                      "a result. Congrats! <@U0000000001>", text)
        self.assertNotIn("*Our paper:*", text)
        self.assertNotIn("*Congrats:*", text)
        self.assertNotIn("*Authors:*", text)

    def test_lab_paper_never_mentions_the_pi(self):
        client = FakeClient({'lab_context': 'Our own result.',
                             'mention_ids': ['', 'U0000000001']})
        result = enrich(self.lab_paper(), client, roster=ROSTER_WITH_PI)
        self.assertEqual(result['mention_ids'], ['U0000000001'])

    def test_two_lab_authors_can_both_be_mentioned_but_no_more(self):
        article = make_article(creators=[
            author('Example', 'Person'), author('Another', 'Example'),
            author('Third', 'Example'),
        ])
        client = FakeClient({
            'lab_context': 'Three of ours report a result. Congrats!',
            'mention_ids': ['U0000000001', 'U0000000002', 'U0000000003'],
        })
        result = enrich(article, client, roster=ROSTER_WITH_PI)
        self.assertEqual(result['mention_ids'],
                         ['U0000000001', 'U0000000002'])

    def test_collaboration_discards_every_mention(self):
        """Requirement 13."""
        article = make_article(creators=[author('Jane', 'Doe'),
                                         author('Example', 'Chief')])
        client = FakeClient({
            'lab_context': 'In this collaboration, we show something.',
            'mention_ids': ['U0000000001', 'U0000000002'],
        })
        result = enrich(article, client, roster=ROSTER_WITH_PI)
        self.assertEqual(result['mention_ids'], [])

        text = zotbot.format_article(article, result)
        self.assertIn("*Lab context:* In this collaboration, we show "
                      "something.\n", text)
        self.assertNotIn("<@", text)

    def test_external_paper_can_still_reach_any_notifyable_member(self):
        """Requirement 15: unchanged routing for outside papers."""
        client = FakeClient({'lab_context': 'Bears on our encoding models.',
                             'mention_ids': ['U0000000003']})
        result = enrich(make_article(), client, roster=ROSTER_WITH_PI)
        self.assertEqual(result['mention_ids'], ['U0000000003'])

    def test_a_notify_false_member_is_never_mentioned_externally(self):
        client = FakeClient({'lab_context': 'Bears on our encoding models.',
                             'mention_ids': ['']})
        result = enrich(make_article(), client, roster=ROSTER_WITH_PI)
        self.assertEqual(result['mention_ids'], [])


class AuthorContextPayloadTests(unittest.TestCase):

    def test_request_carries_mode_author_order_and_lab_authors(self):
        """Requirement 14."""
        article = make_article(creators=[
            author('Example', 'Person'),
            author('Bob', 'Editor', creator_type='editor'),
            author('Jane', 'Doe'),
            author('Example', 'Chief'),
        ])
        client = FakeClient({'lab_context': 'Ours.', 'mention_ids': []})
        enrich(article, client, roster=ROSTER_WITH_PI)

        sent = sent_payload(client)
        self.assertEqual(sent['paper_mode'], 'lab')
        self.assertEqual(sent['paper']['authors'],
                         ['Example Person', 'Jane Doe', 'Example Chief'])
        self.assertEqual(sent['lab_authors'], [
            {'name': 'Example Person', 'author_position': 1,
             'role': 'member', 'notify': True, 'slack_id': 'U0000000001'},
            {'name': 'Example Chief', 'author_position': 3,
             'role': 'pi', 'notify': False, 'slack_id': ''},
        ])

    def test_external_request_says_so_and_carries_no_lab_authors(self):
        client = FakeClient({'lab_context': 'Useful method.', 'mention_ids': []})
        enrich(make_article(), client, roster=ROSTER_WITH_PI)

        sent = sent_payload(client)
        self.assertEqual(sent['paper_mode'], 'external')
        self.assertEqual(sent['lab_authors'], [])
        self.assertEqual(sent['paper']['authors'], ['Jane Doe', 'John Roe'])

    def test_collaboration_request_says_so(self):
        client = FakeClient({'lab_context': 'Our collaboration.',
                             'mention_ids': []})
        enrich(make_article(creators=[author('Example', 'Chief')]),
               client, roster=ROSTER_WITH_PI)
        self.assertEqual(sent_payload(client)['paper_mode'], 'collaboration')

    def test_authors_are_omitted_when_absent(self):
        client = FakeClient({'lab_context': 'Useful method.', 'mention_ids': []})
        enrich(make_article(creators=[]), client)
        self.assertNotIn('authors', sent_payload(client)['paper'])

    def test_instructions_cover_all_three_modes(self):
        for mode in ('external', 'lab', 'collaboration'):
            self.assertIn(f'paper_mode "{mode}"', zotbot.ENRICHMENT_INSTRUCTIONS)

    def test_one_openai_call_per_paper_in_every_mode(self):
        for creators in ([author('Jane', 'Doe')],
                         [author('Example', 'Person')],
                         [author('Example', 'Chief')]):
            client = FakeClient({'lab_context': 'Something.', 'mention_ids': []})
            enrich(make_article(creators=creators), client,
                   roster=ROSTER_WITH_PI)
            self.assertEqual(len(client.calls), 1)


if __name__ == '__main__':
    unittest.main()
