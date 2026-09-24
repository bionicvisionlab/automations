#!/usr/bin/env python

"""Tests for the journal-club promotion job.

    python -m unittest test_journal_club -v

Slack and Zotero are one in-memory fake behind a patched `requests`, so
nothing here hits the network.
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse

import journal_club


NOW = 1_790_000_000.0
JC = "JCLUB111"          # Journal Club collection key
NEW = "NEWCOLL2"         # the NEW collection
OTHER = "OTHERCO3"       # some unrelated collection


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

    The map starts as whatever zotbot_post() messages ZotBot "recorded";
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
        self.items = items or {}              # key -> {'version', 'collections'}
        self.page_size = page_size
        self.patches = []                     # (key, json, headers)
        self.posts = []                       # chat.postMessage payloads
        self.history_params = []
        self.conflicts = {}                   # key -> 412s still to return
        self.broken = set()                   # keys whose GET fails

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
        key = path.rsplit("/", 1)[-1]
        if key in self.broken or key not in self.items:
            return FakeResponse(404, {})
        item = self.items[key]
        return FakeResponse(body={
            "key": key, "version": item["version"],
            "data": {"key": key, "title": "T",
                     "collections": list(item["collections"])},
        })

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
             mock.patch("builtins.print"):
            return journal_club.main("xoxb-test", "CPAPERS", "12345", "zkey",
                                     JC, self.mapping, dry_run=dry_run, now=NOW)


def slack_message(ts, reactions=(), text="*A paper*"):
    message = {"type": "message", "ts": ts, "text": text}
    if reactions:
        message["reactions"] = [
            {"name": name, "count": count, "users": [f"U{i}" for i in range(count)]}
            for name, count in reactions
        ]
    return message


def zotbot_post(key, ts, reactions=(), text="*A paper*"):
    """A ZotBot post, plus the ts -> key entry ZotBot recorded for it."""
    return {**slack_message(ts, reactions, text), "_recorded_key": key}


def item(collections=(NEW,), version=100):
    return {"version": version, "collections": list(collections)}


class ThresholdTests(unittest.TestCase):

    def test_fewer_than_three_reactions_write_nothing(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                reactions = [("chefs_kiss", count)] if count else []
                world = FakeWorld([zotbot_post("ABCD2345", "1.1", reactions)],
                                  {"ABCD2345": item()})
                promoted, failures = world.run()
                self.assertEqual((promoted, failures), ([], 0))
                self.assertEqual(world.patches, [])
                self.assertEqual(world.posts, [])

    def test_three_or_more_reactions_promote(self):
        for count in (3, 7):
            with self.subTest(count=count):
                world = FakeWorld(
                    [zotbot_post("ABCD2345", "1.1", [("chefs_kiss", count)])],
                    {"ABCD2345": item()})
                promoted, _ = world.run()
                self.assertEqual(promoted, ["ABCD2345"])
                self.assertIn(JC, world.items["ABCD2345"]["collections"])

    def test_paper_without_the_ai_nomination_line_can_be_promoted(self):
        text = "*A paper*\n*Lab context:* Something.\n\n*Abstract:*\n```...```"
        self.assertNotIn("Nominate", text)
        world = FakeWorld(
            [zotbot_post("ABCD2345", "1.1", [("chefs_kiss", 3)], text=text)],
            {"ABCD2345": item()})
        promoted, _ = world.run()
        self.assertEqual(promoted, ["ABCD2345"])

    def test_unrelated_emoji_do_not_count(self):
        world = FakeWorld([zotbot_post("ABCD2345", "1.1", [
            ("chefs_kiss", 2), ("+1", 9), ("chefs_kiss::skin-tone-2", 4),
            ("fire", 5),
        ])], {"ABCD2345": item()})
        promoted, _ = world.run()
        self.assertEqual(promoted, [])
        self.assertEqual(world.patches, [])


class MessageSelectionTests(unittest.TestCase):

    def test_the_recorded_ts_decides_which_item_is_promoted(self):
        # The text names another paper; only the artifact entry counts.
        world = FakeWorld(
            [zotbot_post("ABCD2345", "1.1", [("chefs_kiss", 3)],
                         text="<https://doi.org/x|*EFGH2345*>")],
            {"ABCD2345": item(), "EFGH2345": item()})
        promoted, _ = world.run()
        self.assertEqual(promoted, ["ABCD2345"])
        self.assertEqual([p[0] for p in world.patches], ["ABCD2345"])

    def test_messages_absent_from_the_artifact_cannot_promote(self):
        votes = [("chefs_kiss", 5)]
        world = FakeWorld([
            slack_message("1.1", votes, text="ABCD2345"),     # someone's post
            slack_message("1.2", votes, text="*A paper*"),    # lookalike
        ], {"ABCD2345": item()})
        world.mapping["9.9"] = "ABCD2345"   # recorded, but a different ts
        promoted, failures = world.run()
        self.assertEqual((promoted, failures), ([], 0))
        self.assertEqual(world.patches, [])
        self.assertEqual(world.posts, [])

    def test_malformed_recorded_keys_are_ignored(self):
        votes = [("chefs_kiss", 5)]
        world = FakeWorld([
            zotbot_post(k, f"1.{i + 1}", votes)
            for i, k in enumerate(["", "abcd2345", "ABCD/../X",
                                   "TOOLONGKEY", 12345678])
        ], {"ABCD2345": item()})
        promoted, failures = world.run()
        self.assertEqual((promoted, failures), ([], 0))
        self.assertEqual(world.patches, [])

    def test_history_request_covers_90_days_without_metadata(self):
        world = FakeWorld([])
        world.run()
        params = world.history_params[0]
        self.assertEqual(params["channel"], "CPAPERS")
        self.assertNotIn("include_all_metadata", params)
        self.assertAlmostEqual(float(params["oldest"]), NOW - 90 * 86400)

    def test_nominations_on_later_pages_are_found(self):
        messages = [zotbot_post(f"KEY{i}AAAA", f"1.{i}") for i in range(2, 7)]
        messages.append(zotbot_post("LATE2345", "2.0", [("chefs_kiss", 3)]))
        world = FakeWorld(messages, {"LATE2345": item()}, page_size=2)
        promoted, _ = world.run()
        self.assertEqual(promoted, ["LATE2345"])
        self.assertGreater(len(world.history_params), 1)


class PromotionTests(unittest.TestCase):

    def nominated(self, collections=(NEW, OTHER)):
        return FakeWorld(
            [zotbot_post("ABCD2345", "1700000000.000100", [("chefs_kiss", 3)])],
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

    def test_first_promotion_posts_one_thread_confirmation(self):
        world = self.nominated()
        world.run()
        self.assertEqual(world.posts, [{
            "channel": "CPAPERS", "thread_ts": "1700000000.000100",
            "text": ":chefs_kiss: Added to Journal Club.",
        }])

    def test_already_in_journal_club_means_no_patch_and_no_confirmation(self):
        world = self.nominated(collections=(NEW, JC))
        promoted, failures = world.run()
        self.assertEqual((promoted, failures), ([], 0))
        self.assertEqual(world.patches, [])
        self.assertEqual(world.posts, [])

    def test_second_run_is_a_no_op(self):
        world = self.nominated()
        world.run()
        world.run()
        self.assertEqual(len(world.patches), 1)
        self.assertEqual(len(world.posts), 1)

    def test_one_version_conflict_refetches_and_retries_once(self):
        world = self.nominated()
        world.conflicts["ABCD2345"] = 1
        promoted, failures = world.run()
        self.assertEqual((promoted, failures), (["ABCD2345"], 0))
        self.assertEqual(len(world.patches), 2)
        # The retry carries the refetched version, not the stale one.
        self.assertEqual(world.patches[0][2]["If-Unmodified-Since-Version"], "100")
        self.assertEqual(world.patches[1][2]["If-Unmodified-Since-Version"], "101")
        self.assertEqual(world.items["ABCD2345"]["collections"], [NEW, OTHER, JC])
        self.assertEqual(len(world.posts), 1)

    def test_a_second_conflict_gives_up_without_confirming(self):
        world = self.nominated()
        world.conflicts["ABCD2345"] = 2
        promoted, failures = world.run()
        self.assertEqual((promoted, failures), ([], 1))
        self.assertEqual(len(world.patches), 2)
        self.assertEqual(world.posts, [])
        self.assertNotIn(JC, world.items["ABCD2345"]["collections"])

    def test_one_failing_paper_does_not_affect_another(self):
        world = FakeWorld([
            zotbot_post("BAD22345", "1.1", [("chefs_kiss", 4)]),
            zotbot_post("GREAT234", "1.2", [("chefs_kiss", 3)]),
        ], {"BAD22345": item([OTHER]), "GREAT234": item()})
        world.broken.add("BAD22345")

        promoted, failures = world.run()
        self.assertEqual((promoted, failures), (["GREAT234"], 1))
        self.assertEqual(world.items["BAD22345"]["collections"], [OTHER])
        self.assertEqual(world.items["GREAT234"]["collections"], [NEW, JC])
        self.assertEqual([p["thread_ts"] for p in world.posts], ["1.2"])


class DryRunTests(unittest.TestCase):

    def test_dry_run_reports_but_never_writes(self):
        world = FakeWorld([
            zotbot_post("ABCD2345", "1.1", [("chefs_kiss", 3)]),
            zotbot_post("DUNE2345", "1.2", [("chefs_kiss", 3)]),
        ], {"ABCD2345": item(), "DUNE2345": item([NEW, JC])})

        with mock.patch.object(journal_club, "requests", world), \
             mock.patch("builtins.print") as printed:
            promoted, _ = journal_club.main("t", "CPAPERS", "1", "z", JC,
                                            world.mapping, dry_run=True, now=NOW)

        self.assertEqual(promoted, ["ABCD2345"])
        self.assertEqual(world.patches, [])
        self.assertEqual(world.posts, [])
        logged = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn("ABCD2345", logged)
        self.assertNotIn("DUNE2345", logged)


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
            [slack_message("1789999000.000100", [("chefs_kiss", 3)])],
            {"ABCD2345": item()})
        with mock.patch("builtins.print"):
            world.mapping = journal_club.load_messages(path)
        promoted, _ = world.run()
        self.assertEqual(promoted, ["ABCD2345"])

    def test_old_missing_or_broken_artifacts_promote_nothing(self):
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
        import zotbot
        self.assertEqual(journal_club.REACTION, zotbot.JOURNAL_CLUB_REACTION)
        self.assertEqual(journal_club.LOOKBACK_DAYS,
                         zotbot.MESSAGE_RETENTION_DAYS)


if __name__ == "__main__":
    unittest.main()
