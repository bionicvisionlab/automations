#!/usr/bin/env python

"""
Promote nominated #papers posts to the Zotero Journal Club collection.

Runs once a day. Scans the last LOOKBACK_DAYS of #papers for ZotBot messages
(recognized by their invisible metadata) with at least THRESHOLD :chefs_kiss:
reactions, adds the same Zotero item to the Journal Club collection while
keeping every collection it is already in, and says so in the thread.

Slack and Zotero are the only state: a paper already in Journal Club is left
alone, so repeated runs are no-ops.

    python journal_club.py [--dry-run]

Reads SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, ZOTERO_GROUP, ZOTERO_API_KEY and
ZOTERO_JOURNAL_CLUB_COLLECTION from the environment.
"""

import os
import re
import sys
import time

import requests


REACTION = "chefs_kiss"
THRESHOLD = 3
LOOKBACK_DAYS = 90

SLACK_EVENT_TYPE = "bvl.zotbot_paper"  # as posted by zotbot.py
CONFIRMATION = ":chefs_kiss: Added to Journal Club."
TIMEOUT = 30

# Zotero object keys: eight characters from this alphabet.
ZOTERO_KEY = re.compile(r"[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}")


def slack(method, token, http_method="get", **params):
    """Call one Slack Web API method; raise unless it answers ok."""
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {token}"}
    if http_method == "get":
        resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    else:
        resp = requests.post(url, headers=headers, json=params, timeout=TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {body.get('error')}")
    return body


def item_key(message):
    """Zotero item key from a ZotBot message's metadata, or None."""
    metadata = message.get("metadata") or {}
    if metadata.get("event_type") != SLACK_EVENT_TYPE:
        return None
    key = (metadata.get("event_payload") or {}).get("zotero_item_key")
    if isinstance(key, str) and ZOTERO_KEY.fullmatch(key):
        return key
    return None


def votes(message):
    """Number of :chefs_kiss: reactions on a message."""
    for reaction in message.get("reactions") or []:
        if reaction.get("name") == REACTION:
            return reaction.get("count") or 0
    return 0


def nominations(token, channel_id, now=None):
    """(item_key, ts) of every recent ZotBot post at or above THRESHOLD.

    Whether ZotBot showed the nomination line plays no part here.
    """
    oldest = (now if now is not None else time.time()) - LOOKBACK_DAYS * 86400
    params = {
        "channel": channel_id,
        "oldest": f"{oldest:.6f}",
        "include_all_metadata": "true",
        "limit": 200,
    }
    while True:
        body = slack("conversations.history", token, **params)
        for message in body.get("messages") or []:
            key = item_key(message)
            if key and votes(message) >= THRESHOLD:
                yield key, message["ts"]
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return
        params["cursor"] = cursor


def add_to_collection(group, api_key, key, collection, dry_run=False):
    """Add one Zotero item to collection, keeping all its other collections.

    Returns True if the item was (or, in a dry run, would be) newly added and
    False if it was already there. Zotero replaces the whole collections list
    on a write, hence the read-modify-write guarded by the item version. A
    version conflict gets exactly one refetch and retry.
    """
    url = f"https://api.zotero.org/groups/{group}/items/{key}"
    headers = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3"}

    for attempt in range(2):
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        item = resp.json()
        collections = list(item["data"].get("collections") or [])
        if collection in collections:
            return False
        if dry_run:
            return True

        resp = requests.patch(
            url,
            headers={**headers,
                     "If-Unmodified-Since-Version": str(item["version"])},
            json={"collections": collections + [collection]},
            timeout=TIMEOUT,
        )
        if resp.status_code == 412 and attempt == 0:
            print(f"{key}: version conflict, retrying once")
            continue
        resp.raise_for_status()
        return True


def main(slack_token, channel_id, zotero_group, zotero_api_key, collection,
         dry_run=False, now=None):
    """Promote every qualifying paper. Returns (promoted keys, failure count).

    A failure on one paper is logged and does not stop the others.
    """
    promoted, failures, seen = [], 0, set()

    for key, ts in nominations(slack_token, channel_id, now=now):
        if key in seen:
            continue
        seen.add(key)
        try:
            if not add_to_collection(zotero_group, zotero_api_key, key,
                                     collection, dry_run=dry_run):
                continue
            promoted.append(key)
            if dry_run:
                print(f"[DRY RUN] would promote {key}")
                continue
            print(f"Promoted {key} to Journal Club")
            slack("chat.postMessage", slack_token, http_method="post",
                  channel=channel_id, thread_ts=ts, text=CONFIRMATION)
        except Exception as e:
            failures += 1
            print(f"Error promoting {key}: {type(e).__name__}: {e}")

    print(f"{len(promoted)} paper(s) {'would be ' if dry_run else ''}"
          f"promoted, {failures} failure(s)")
    return promoted, failures


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Read Slack and Zotero, but write nothing")
    args = parser.parse_args()

    names = ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "ZOTERO_GROUP",
             "ZOTERO_API_KEY", "ZOTERO_JOURNAL_CLUB_COLLECTION")
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        parser.error("missing environment: " + ", ".join(missing))

    _, failures = main(*(os.environ[n] for n in names), dry_run=args.dry_run)
    if failures:
        sys.exit(1)
