#!/usr/bin/env python

"""Tests for the journal-club promotion job.

    python -m unittest test_journal_club -v

Slack and Zotero are one in-memory fake behind a patched `requests`, so
nothing here hits the network. Posts are rendered by zotbot.format_article(),
so these tests also pin what journal_club.py can parse.
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse

import journal_club
import zotbot


NOW = 1_790_000_000.0
GROUP = "12345"
JC = "JCLUB111"          # Journal Club collection key
NEW = "NEWCOLL2"         # the NEW collection
OTHER = "OTHERCO3"       # some unrelated collection
ZOTBOT = "BZOTBOT01"     # bot_id of ZotBot's posts


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeWorld:
    """#papers history, zotbot.json's message map and a Zotero group library,
    with write logs.

    The map starts as whatever mapped_post() messages ZotBot "recorded";
    Slack itself never sees those keys.
    """

    def __init__(self, messages=(), items=None, page_size=None):
        self.messages, self.mapping = [], {}
        for message in messages:
            message = dict(message)
            key = message.pop("_recorded_key", None)
            if key is not None:
                self.mapping[message["ts"]] = key
            self.messages.append(message)
        self.items = items or {}    # key -> {version, collections, title, DOI}
        self.page_size = page_size
        self.patches = []                     # (key, json, headers)
        self.posts = []                       # chat.postMessage payloads
        self.history_params = []
        self.searches = []                    # Zotero search params
        self.conflicts = {}                   # key -> 412s still to return
        self.broken = set()                   # keys whose GET fails
        self.writable = True                  # the Zotero key's write access

    # requests.get
    def get(self, url, headers=None, params=None, timeout=None):
        path = urlparse(url).path
        if path == "/api/conversations.history":
            self.history_params.append(dict(params))
            start = int(params.get("cursor") or 0)
            size = self.page_size or len(self.messages) or 1
            page = self.messages[start:start + size]
            nxt = start + size if start + size < len(self.messages) else None
            return FakeResponse(body={
                "ok": True, "messages": page,
                "response_metadata": {"next_cursor": str(nxt) if nxt else ""},
            })
        if path == f"/groups/{GROUP}/items/top":
            return self.search(params)
        if path == "/keys/current":
            return FakeResponse(body={"key": "zkey", "access": {"groups": {
                GROUP: {"library": True, "write": self.writable}}}})
        key = path.rsplit("/", 1)[-1]
        if key in self.broken or key not in self.items:
            return FakeResponse(404, {})
        return FakeResponse(body=self.item_json(key))

    def item_json(self, key):
        item = self.items[key]
        return {"key": key, "version": item["version"],
                "data": {"key": key, "title": item.get("title", "T"),
                         "DOI": item.get("DOI", ""),
                         "collections": list(item["collections"])}}

    def search(self, params):
        """Zotero quick search: loose, as the real one is. Every word of q in
        the title, or with qmode=everything, q anywhere."""
        self.searches.append(dict(params))
        q = params["q"].casefold()
        hits = []
        for key, item in self.items.items():
            title = item.get("title", "").casefold()
            if params.get("qmode") == "everything":
                hit = q in title or q in item.get("DOI", "").casefold()
            else:
                hit = all(word in title for word in q.split())
            if hit:
                hits.append(self.item_json(key))
        return FakeResponse(body=hits)

    # requests.patch
    def patch(self, url, headers=None, json=None, timeout=None):
        key = urlparse(url).path.rsplit("/", 1)[-1]
        self.patches.append((key, json, headers))
        item = self.items[key]
        if self.conflicts.get(key):
            self.conflicts[key] -= 1
            item["version"] += 1          # someone else edited it meanwhile
            return FakeResponse(412)
        if headers.get("If-Unmodified-Since-Version") != str(item["version"]):
            return FakeResponse(412)
        item["collections"] = list(json["collections"])
        item["version"] += 1
        return FakeResponse(204)

    # requests.post
    def post(self, url, headers=None, json=None, timeout=None):
        assert urlparse(url).path == "/api/chat.postMessage"
        self.posts.append(json)
        return FakeResponse(body={"ok": True, "ts": "999.1"})

    def run(self, dry_run=False):
        with mock.patch.object(journal_club, "requests", self), \
             mock.patch("builtins.print") as printed:
            summary = journal_club.main("xoxb-test", "CPAPERS", GROUP, "zkey",
                                        JC, self.mapping, dry_run=dry_run,
                                        now=NOW)
        self.log = "\n".join(" ".join(map(str, c.args))
                             for c in printed.call_args_list)
        return summary


def reactions_of(reactions):
    return [{"name": name, "count": count,
             "users": [f"U{i}" for i in range(count)]}
            for name, count in reactions]


def announcement(key="ABCD2345", title="A Paper", doi="10.1234/paper",
                 group_id=None):
    """The text ZotBot posts for this paper."""
    return zotbot.format_article({
        "data": {"key": key, "title": title, "DOI": doi,
                 "itemType": "journalArticle", "publicationTitle": "J",
                 "date": "2026", "abstractNote": "Short.", "tags": []},
        "meta": {"creatorSummary": "Doe et al.",
                 "createdByUser": {"username": "someone"}},
    }, group_id=group_id)


def slack_message(ts, reactions=(), text="*A paper*", **extra):
    """A message from a person unless extra says otherwise."""
    message = {"type": "message", "ts": ts, "text": text, **extra}
    if reactions:
        message["reactions"] = reactions_of(reactions)
    return message


def linked_post(key, ts, reactions=(), group_id=GROUP, **paper):
    """A current ZotBot post, which links its Zotero item."""
    return slack_message(ts, reactions, bot_id=ZOTBOT,
                         text=announcement(key, group_id=group_id, **paper))


def mapped_post(key, ts, reactions=(), text=None):
    """A ZotBot post from before the link, plus the ts -> key entry ZotBot
    recorded for it."""
    return {**slack_message(ts, reactions, bot_id=ZOTBOT,
                            text=text or announcement(key)),
            "_recorded_key": key}


def legacy_post(ts, reactions=(), **paper):
    """A webhook-era ZotBot post: neither linked nor recorded."""
    return slack_message(ts, reactions, bot_id="BWEBHOOK",
                         subtype="bot_message", username="ZotBot",
                         text=announcement(**paper))


def item(collections=(NEW,), version=100, title="A Paper", doi="10.1234/paper"):
    return {"version": version, "collections": list(collections),
            "title": title, "DOI": doi}


KISS = [("chefs_kiss", 1)]


class ResolutionTests(unittest.TestCase):
    """One :chefs_kiss: is enough, however the post maps to Zotero."""

    def assert_promoted_once(self, world, key, ts):
        summary = world.run()
        self.assertEqual(summary["promoted"], [key])
        self.assertEqual(summary["unresolved"], [])
        self.assertEqual(world.items[key]["collections"], [NEW, JC])
        self.assertEqual(world.posts, [{
            "channel": "CPAPERS", "thread_ts": ts,
            "text": ":chefs_kiss: <@U0> recommended this paper for journal "
                    "club, so it was added to the Journal Club collection.",
        }])
        return summary

    def test_new_post_resolves_through_its_zotero_link(self):
        world = FakeWorld([linked_post("ABCD2345", "1.1", KISS)],
                          {"ABCD2345": item()})
        self.assert_promoted_once(world, "ABCD2345", "1.1")
        self.assertEqual(world.searches, [])
        self.assertIn("via link", world.log)

    def test_link_wins_over_text_and_needs_no_map(self):
        # Same title and DOI as another item: the link alone decides.
        world = FakeWorld([linked_post("ABCD2345", "1.1", KISS)],
                          {"ABCD2345": item(), "EFGH2345": item()})
        self.assert_promoted_once(world, "ABCD2345", "1.1")
        self.assertEqual(world.mapping, {})

    def test_older_post_resolves_through_zotbot_json(self):
        world = FakeWorld([mapped_post("ABCD2345", "1.1", KISS)],
                          {"ABCD2345": item()})
        self.assert_promoted_once(world, "ABCD2345", "1.1")
        self.assertEqual(world.searches, [])
        self.assertIn("via zotbot.json", world.log)

    def test_legacy_post_resolves_by_doi(self):
        world = FakeWorld(
            [legacy_post("1.1", KISS, doi="10.1234/PAPER")],
            {"ABCD2345": item(doi="https://doi.org/10.1234/paper"),
             # Same title, different DOI: a different paper.
             "EFGH2345": item(doi="10.1234/other")})
        self.assert_promoted_once(world, "ABCD2345", "1.1")
        self.assertIn("via DOI", world.log)

    def test_legacy_post_without_doi_resolves_by_exact_title(self):
        world = FakeWorld(
            [legacy_post("1.1", KISS, title="Seeing  & Blindness", doi="")],
            {"ABCD2345": item(title="Seeing & blindness", doi=""),
             "EFGH2345": item(title="Seeing & Blindness Revisited", doi="")})
        # Slack stores the & escaped.
        world.messages[0]["text"] = world.messages[0]["text"].replace("&", "&amp;")
        self.assert_promoted_once(world, "ABCD2345", "1.1")
        self.assertIn("via title", world.log)

    def test_three_nominated_papers_are_all_processed(self):
        world = FakeWorld([
            linked_post("LINK2345", "3.0", KISS, title="Linked", doi="10.1/l"),
            mapped_post("MAPD2345", "2.0", KISS),
            legacy_post("1.0", KISS, title="Old one", doi="10.1/old"),
        ], {"LINK2345": item(title="Linked", doi="10.1/l"),
            "MAPD2345": item(title="Mapped", doi="10.1/m"),
            "LEGA2345": item(title="Old one", doi="10.1/old")})
        summary = world.run()
        self.assertEqual(sorted(summary["promoted"]),
                         ["LEGA2345", "LINK2345", "MAPD2345"])
        self.assertEqual(sorted(p["thread_ts"] for p in world.posts),
                         ["1.0", "2.0", "3.0"])
        self.assertEqual((summary["nominated"], summary["resolved"]), (3, 3))
        self.assertIn("Nominated ZotBot posts: 3", world.log)
        self.assertIn("Newly promoted: 3", world.log)


class UnresolvedTests(unittest.TestCase):

    def assert_unresolved(self, world, reason):
        summary = world.run()
        self.assertEqual(summary["unresolved"], ["1.1"])
        self.assertEqual(summary["promoted"], [])
        self.assertEqual(world.patches, [])
        self.assertEqual(world.posts, [])
        self.assertIn("UNRESOLVED post 1.1", world.log)
        self.assertIn(reason, world.log)
        self.assertIn("Unresolved nominations: 1", world.log)

    def test_two_items_with_the_doi_are_ambiguous(self):
        world = FakeWorld([legacy_post("1.1", KISS)],
                          {"ABCD2345": item(), "DUPE2345": item()})
        self.assert_unresolved(world, "2 Zotero items share this DOI")

    def test_no_matching_item_is_reported(self):
        # The search finds a lookalike; its fields do not match.
        world = FakeWorld([legacy_post("1.1", KISS, title="A Paper", doi="")],
                          {"ABCD2345": item(title="A Paper, Part 2", doi="")})
        self.assert_unresolved(world, "no Zotero item with this title")

    def test_link_to_another_group_is_not_followed(self):
        world = FakeWorld([linked_post("ABCD2345", "1.1", KISS, group_id="999")],
                          {"ABCD2345": item()})
        self.assert_unresolved(world, "another group")

    def test_log_quotes_neither_title_nor_doi(self):
        world = FakeWorld([legacy_post("1.1", KISS, title="Secret Title",
                                       doi="10.9/secret")], {})
        world.run()
        self.assertNotIn("Secret", world.log)
        self.assertNotIn("10.9/secret", world.log)

    def test_unresolved_makes_the_run_fail(self):
        world = FakeWorld([legacy_post("1.1", KISS)], {})
        env = {"SLACK_BOT_TOKEN": "t", "SLACK_CHANNEL_ID": "C",
               "ZOTERO_GROUP": GROUP, "ZOTERO_WRITE_API_KEY": "k",
               "ZOTERO_JOURNAL_CLUB_COLLECTION": JC}
        with mock.patch.dict(os.environ, env), \
             mock.patch("sys.argv", ["journal_club.py", "--artifact", "none"]), \
             mock.patch("requests.get", world.get), \
             mock.patch("builtins.print"), \
             self.assertRaises(SystemExit) as exit_:
            import runpy
            runpy.run_path(journal_club.__file__, run_name="__main__")
        self.assertEqual(exit_.exception.code, 1)


class SelectionTests(unittest.TestCase):

    def test_no_reaction_writes_nothing(self):
        world = FakeWorld([linked_post("ABCD2345", "1.1")], {"ABCD2345": item()})
        summary = world.run()
        self.assertEqual(summary["nominated"], 0)
        self.assertEqual(world.patches, [])
        self.assertEqual(world.posts, [])

    def test_paper_without_the_ai_nomination_line_can_be_promoted(self):
        text = announcement("ABCD2345", group_id=GROUP)
        self.assertNotIn("Nominate", text)
        world = FakeWorld([slack_message("1.1", KISS, text=text, bot_id=ZOTBOT)],
                          {"ABCD2345": item()})
        self.assertEqual(world.run()["promoted"], ["ABCD2345"])

    def test_unrelated_emoji_do_not_count(self):
        world = FakeWorld([linked_post("ABCD2345", "1.1", [
            ("+1", 9), ("chefs_kiss::skin-tone-2", 4), ("fire", 5),
        ])], {"ABCD2345": item()})
        self.assertEqual(world.run()["nominated"], 0)
        self.assertEqual(world.patches, [])

    def test_reactions_on_other_messages_are_ignored(self):
        votes = [("chefs_kiss", 5)]
        world = FakeWorld([
            # A person quoting a ZotBot post, Zotero link and all.
            slack_message("1.1", votes, text=announcement(group_id=GROUP)),
            # A person sharing a DOI.
            slack_message("1.2", votes, text="<https://doi.org/10.1234/paper>"),
            # Another bot, without ZotBot's fields.
            slack_message("1.3", votes, bot_id="BOTHER", text="*A Paper*"),
        ], {"ABCD2345": item()})
        world.mapping["9.9"] = "ABCD2345"   # recorded, but a different ts
        summary = world.run()
        self.assertEqual((summary["scanned"], summary["nominated"]), (3, 0))
        self.assertEqual((world.patches, world.posts, world.searches), ([], [], []))

    def test_malformed_recorded_keys_are_not_trusted(self):
        world = FakeWorld([
            slack_message(f"1.{i + 1}", KISS, text="*A paper*")
            for i in range(5)
        ], {"ABCD2345": item()})
        for i, k in enumerate(["", "abcd2345", "ABCD/../X", "TOOLONGKEY", 12345678]):
            world.mapping[f"1.{i + 1}"] = k
        summary = world.run()
        self.assertEqual(summary["nominated"], 0)
        self.assertEqual(world.patches, [])

    def test_history_request_covers_90_days_without_metadata(self):
        world = FakeWorld([])
        world.run()
        params = world.history_params[0]
        self.assertEqual(params["channel"], "CPAPERS")
        self.assertNotIn("include_all_metadata", params)
        self.assertAlmostEqual(float(params["oldest"]), NOW - 90 * 86400)

    def test_nominations_on_later_pages_are_found(self):
        messages = [linked_post(f"KEY{i}AAAA", f"1.{i}") for i in range(2, 7)]
        messages.append(legacy_post("2.0", KISS))
        messages.append(linked_post("LATE2345", "2.1", KISS))
        world = FakeWorld(messages, {"LATE2345": item(doi="10.1/late"),
                                     "ABCD2345": item()}, page_size=2)
        summary = world.run()
        self.assertEqual(sorted(summary["promoted"]), ["ABCD2345", "LATE2345"])
        self.assertEqual(summary["scanned"], 7)
        self.assertGreater(len(world.history_params), 1)


class PromotionTests(unittest.TestCase):

    def nominated(self, collections=(NEW, OTHER)):
        return FakeWorld(
            [linked_post("ABCD2345", "1700000000.000100", [("chefs_kiss", 3)])],
            {"ABCD2345": item(collections)})

    def test_patch_keeps_new_and_every_other_collection(self):
        world = self.nominated()
        world.run()
        self.assertEqual(len(world.patches), 1)
        key, body, headers = world.patches[0]
        self.assertEqual(key, "ABCD2345")
        self.assertEqual(body, {"collections": [NEW, OTHER, JC]})
        self.assertEqual(headers["If-Unmodified-Since-Version"], "100")
        self.assertEqual(world.items["ABCD2345"]["collections"], [NEW, OTHER, JC])

    def test_confirmation_names_every_recommender(self):
        cases = [
            (["U0", "U1"], "<@U0> and <@U1> recommended"),
            (["U0", "U1", "U2"], "<@U0>, <@U1> and <@U2> recommended"),
            ([], "Someone recommended"),
        ]
        for users, expected in cases:
            with self.subTest(users=users):
                self.assertIn(expected, journal_club.confirmation(users))

    def test_already_in_journal_club_means_no_patch_and_no_confirmation(self):
        world = self.nominated(collections=(NEW, JC))
        summary = world.run()
        self.assertEqual((summary["promoted"], summary["already"]), ([], 1))
        self.assertEqual(world.patches, [])
        self.assertEqual(world.posts, [])
        self.assertIn("Already in Journal Club: 1", world.log)

    def test_second_run_is_a_no_op(self):
        world = FakeWorld([
            linked_post("LINK2345", "3.0", KISS, doi="10.1/l"),
            mapped_post("MAPD2345", "2.0", KISS),
            legacy_post("1.0", KISS, doi="10.1/old"),
        ], {"LINK2345": item(doi="10.1/l"), "MAPD2345": item(doi="10.1/m"),
            "LEGA2345": item(doi="10.1/old")})
        world.run()
        second = world.run()
        self.assertEqual((second["promoted"], second["already"]), ([], 3))
        self.assertEqual(len(world.patches), 3)
        self.assertEqual(len(world.posts), 3)

    def test_two_posts_of_one_paper_promote_it_once(self):
        world = FakeWorld([linked_post("ABCD2345", "1.1", KISS),
                           mapped_post("ABCD2345", "1.2", KISS)],
                          {"ABCD2345": item()})
        world.run()
        self.assertEqual(len(world.patches), 1)
        self.assertEqual(len(world.posts), 1)

    def test_one_version_conflict_refetches_and_retries_once(self):
        world = self.nominated()
        world.conflicts["ABCD2345"] = 1
        summary = world.run()
        self.assertEqual((summary["promoted"], summary["failures"]), (["ABCD2345"], 0))
        self.assertEqual(len(world.patches), 2)
        # The retry carries the refetched version, not the stale one.
        self.assertEqual(world.patches[0][2]["If-Unmodified-Since-Version"], "100")
        self.assertEqual(world.patches[1][2]["If-Unmodified-Since-Version"], "101")
        self.assertEqual(world.items["ABCD2345"]["collections"], [NEW, OTHER, JC])
        self.assertEqual(len(world.posts), 1)

    def test_a_second_conflict_gives_up_without_confirming(self):
        world = self.nominated()
        world.conflicts["ABCD2345"] = 2
        summary = world.run()
        self.assertEqual((summary["promoted"], summary["failures"]), ([], 1))
        self.assertEqual(len(world.patches), 2)
        self.assertEqual(world.posts, [])
        self.assertNotIn(JC, world.items["ABCD2345"]["collections"])

    def test_one_failing_paper_does_not_affect_another(self):
        world = FakeWorld([
            linked_post("BAD22345", "1.1", [("chefs_kiss", 4)]),
            legacy_post("1.2", [("chefs_kiss", 4)], title="Gone", doi=""),
            linked_post("GREAT234", "1.3", [("chefs_kiss", 3)]),
        ], {"BAD22345": item([OTHER]), "GREAT234": item()})
        world.broken.add("BAD22345")

        summary = world.run()
        self.assertEqual((summary["promoted"], summary["failures"],
                          summary["unresolved"]), (["GREAT234"], 1, ["1.2"]))
        self.assertEqual(world.items["BAD22345"]["collections"], [OTHER])
        self.assertEqual(world.items["GREAT234"]["collections"], [NEW, JC])
        self.assertEqual([p["thread_ts"] for p in world.posts], ["1.3"])


class DryRunTests(unittest.TestCase):

    def test_dry_run_reports_outcomes_but_never_writes(self):
        world = FakeWorld([
            linked_post("ABCD2345", "1.1", [("chefs_kiss", 3)]),
            linked_post("DUNE2345", "1.2", [("chefs_kiss", 3)]),
            legacy_post("1.3", KISS, title="Lost", doi=""),
        ], {"ABCD2345": item(), "DUNE2345": item([NEW, JC])})

        summary = world.run(dry_run=True)

        self.assertEqual(summary["promoted"], ["ABCD2345"])
        self.assertEqual(world.patches, [])
        self.assertEqual(world.posts, [])
        self.assertIn("[DRY RUN] ABCD2345 (post 1.1", world.log)
        self.assertIn("DUNE2345 (post 1.2", world.log)
        self.assertIn("already in Journal Club", world.log)
        self.assertIn("UNRESOLVED post 1.3", world.log)
        self.assertIn("Newly promoted: 1 (dry run", world.log)
        self.assertIn("can write to the group library: yes", world.log)
        self.assertEqual(summary["failures"], 0)

    def test_dry_run_fails_when_the_key_cannot_write(self):
        world = FakeWorld([linked_post("ABCD2345", "1.1", KISS)],
                          {"ABCD2345": item()})
        world.writable = False
        summary = world.run(dry_run=True)
        self.assertEqual(summary["failures"], 1)
        self.assertIn("can write to the group library: NO", world.log)


class ArtifactTests(unittest.TestCase):

    def write(self, content):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "zotbot.json")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_promotion_resolves_through_zotbot_json(self):
        path = self.write(json.dumps({
            "time": "2026-09-24T18:23:53Z", "version": 28808,
            "articles_cnt": 1, "skipped": 0,
            "messages": {"1789999000.000100": "ABCD2345"},
        }))
        world = FakeWorld(
            [slack_message("1789999000.000100", [("chefs_kiss", 3)],
                           text=announcement(), bot_id=ZOTBOT)],
            {"ABCD2345": item()})
        with mock.patch("builtins.print"):
            world.mapping = journal_club.load_messages(path)
        self.assertEqual(world.run()["promoted"], ["ABCD2345"])

    def test_old_missing_or_broken_artifacts_give_an_empty_map(self):
        paths = [
            self.write('{"time": "2026-09-24T18:23:53Z", "version": 28808}'),
            self.write('{"messages": ["not", "a", "map"]}'),
            self.write('not json'),
            os.path.join(tempfile.gettempdir(), "no-such-dir", "zotbot.json"),
        ]
        for path in paths:
            with self.subTest(path=path), mock.patch("builtins.print"):
                self.assertEqual(journal_club.load_messages(path), {})


class ZotBotContractTests(unittest.TestCase):

    def test_matches_what_zotbot_records(self):
        self.assertEqual(journal_club.REACTION, zotbot.JOURNAL_CLUB_REACTION)
        self.assertEqual(journal_club.LOOKBACK_DAYS,
                         zotbot.MESSAGE_RETENTION_DAYS)


if __name__ == "__main__":
    unittest.main()
