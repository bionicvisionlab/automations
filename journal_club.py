#!/usr/bin/env python

"""
Promote nominated #papers posts to the Zotero Journal Club collection.

Runs once a day. Scans the last LOOKBACK_DAYS of #papers for ZotBot posts with
at least THRESHOLD :chefs_kiss: reactions, resolves each one to its Zotero item,
adds that item to the Journal Club collection while keeping every collection it
is already in, and says in the thread who recommended it.

A nominated post is resolved, in this order, through
  1. the Zotero item link ZotBot puts in every post,
  2. the {ts: Zotero item key} map that zotbot.py keeps in zotbot.json, for
     posts from before that link, and
  3. the DOI, or failing that the exact title, in the post's heading, looked
     up in the group library, for posts from before the map.
A legacy match must be unique; anything else is reported as unresolved and
leaves Zotero alone. Reactions on messages that are not ZotBot paper posts
never promote anything.

This job writes no state of its own: a paper already in Journal Club is left
alone, so repeated runs are no-ops.

    python journal_club.py [--dry-run] [--artifact zotbot.json]

Reads SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, ZOTERO_GROUP, ZOTERO_WRITE_API_KEY
and ZOTERO_JOURNAL_CLUB_COLLECTION from the environment. Exits 1 if any
nomination failed or could not be resolved.
"""

import datetime
import json
import os
import re
import sys
import time
import unicodedata
from urllib.parse import unquote

import requests


REACTION = "chefs_kiss"
THRESHOLD = 1
LOOKBACK_DAYS = 90

CONFIRMATION = (":chefs_kiss: {who} recommended this paper for journal club, "
                "so it was added to the Journal Club collection.")
TIMEOUT = 30

# Zotero object keys: eight characters from this alphabet.
ZOTERO_KEY = re.compile(r"[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}")

# What zotbot.format_article() writes. Its first line is the heading, the
# title linked to the DOI (or URL) or bare; the Zotero link is newer.
ZOTERO_LINK = re.compile(
    r"<https://www\.zotero\.org/groups/(\d+)/items/([^|>/\s]+)\|Zotero>")
HEADING = re.compile(r"<([^|<>]+)\|\*(.+)\*>|\*(.+)\*")
FIELDS = ("\n*Citation:* ", "\n*Added By:* ")
DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                "http://dx.doi.org/", "doi:")


class Unresolved(Exception):
    """A nominated ZotBot post that maps to no single Zotero item."""


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


def load_messages(artifact):
    """zotbot.json's {Slack ts: Zotero item key} map; {} if there is none."""
    try:
        with open(artifact) as f:
            messages = json.load(f).get("messages")
    except Exception as e:
        print(f"No message map in {artifact}: {type(e).__name__}")
        return {}
    return messages if isinstance(messages, dict) else {}


def item_key(message, messages):
    """Zotero item key ZotBot recorded for this message, or None."""
    key = messages.get(message.get("ts"))
    if isinstance(key, str) and ZOTERO_KEY.fullmatch(key):
        return key
    return None


def votes(message):
    """Number of :chefs_kiss: reactions on a message."""
    for reaction in message.get("reactions") or []:
        if reaction.get("name") == REACTION:
            return reaction.get("count") or 0
    return 0


def voters(message):
    """Slack user IDs behind the :chefs_kiss: reactions, in reaction order."""
    for reaction in message.get("reactions") or []:
        if reaction.get("name") == REACTION:
            return [u for u in reaction.get("users") or [] if isinstance(u, str)]
    return []


def confirmation(users):
    """Thread reply naming whoever recommended the paper."""
    mentions = [f"<@{u}>" for u in users]
    if not mentions:
        who = "Someone"
    elif len(mentions) == 1:
        who = mentions[0]
    else:
        who = ", ".join(mentions[:-1]) + " and " + mentions[-1]
    return CONFIRMATION.format(who=who)


def history(token, channel_id, now=None):
    """Every top-level #papers message from the last LOOKBACK_DAYS."""
    oldest = (now if now is not None else time.time()) - LOOKBACK_DAYS * 86400
    params = {
        "channel": channel_id,
        "oldest": f"{oldest:.6f}",
        "limit": 200,
    }
    while True:
        body = slack("conversations.history", token, **params)
        yield from body.get("messages") or []
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return
        params["cursor"] = cursor


def unescape(text):
    """Undo Slack's escaping of &, < and > in message text."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def heading(text):
    """(title, DOI or None) from a ZotBot post's first line, or None."""
    match = HEADING.fullmatch(text.split("\n", 1)[0].strip())
    if not match:
        return None
    url, linked, bare = match.groups()
    title = unescape(linked or bare).strip()
    doi = normalize_doi(url) if url else None
    return title, (doi or None)


def normalize_title(title):
    return " ".join(unicodedata.normalize("NFKC", title).split()).casefold()


def normalize_doi(value):
    """A bare, casefolded DOI from a DOI or doi.org link; "" if it is neither."""
    value = unquote(unescape(str(value or ""))).strip()
    stripped = True
    while stripped:       # a DOI field may itself hold a doi.org link
        stripped = False
        for prefix in DOI_PREFIXES:
            if value.lower().startswith(prefix):
                value, stripped = value[len(prefix):].strip(), True
    return value.casefold() if value.startswith("10.") else ""


def is_zotbot_post(message, messages):
    """Whether a Slack message is one of ZotBot's paper announcements."""
    if item_key(message, messages):
        return True
    text = message.get("text") or ""
    if not message.get("bot_id"):
        return False
    if ZOTERO_LINK.search(text):
        return True
    return heading(text) is not None and any(f in text for f in FIELDS)


def lookup(group, api_key, title, doi):
    """Key of the one group item matching a legacy post; raise Unresolved.

    Zotero's search only narrows the candidates. A DOI must equal the item's
    DOI field; without one, the normalized title must equal the item's.
    """
    url = f"https://api.zotero.org/groups/{group}/items/top"
    headers = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3"}
    searches = [{"q": title}]
    if doi:
        searches.append({"q": doi, "qmode": "everything"})

    candidates = {}
    for params in searches:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT,
                            params={**params, "limit": 100})
        resp.raise_for_status()
        for item in resp.json():
            candidates[item["key"]] = item.get("data") or {}

    if doi:
        how = "DOI"
        matches = [k for k, d in candidates.items()
                   if normalize_doi(d.get("DOI")) == doi]
    else:
        how = "title"
        matches = [k for k, d in candidates.items()
                   if normalize_title(d.get("title") or "") == normalize_title(title)]
    if not matches:
        raise Unresolved(f"no Zotero item with this {how}")
    if len(matches) > 1:
        raise Unresolved(f"{len(matches)} Zotero items share this {how}: "
                         + ", ".join(sorted(matches)))
    return matches[0], how


def resolve(message, messages, group, api_key):
    """(Zotero item key, how it was found) for a nominated ZotBot post."""
    text = message.get("text") or ""
    link = ZOTERO_LINK.search(text) if message.get("bot_id") else None
    if link:
        linked_group, key = link.groups()
        if linked_group != str(group):
            raise Unresolved("its Zotero link points to another group")
        if not ZOTERO_KEY.fullmatch(key):
            raise Unresolved("its Zotero link has a malformed item key")
        return key, "link"

    key = item_key(message, messages)
    if key:
        return key, "zotbot.json"

    parsed = heading(text)
    if parsed is None:
        raise Unresolved("no paper heading to look up")
    return lookup(group, api_key, *parsed)


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


def can_write(group, api_key):
    """Whether this Zotero key may edit the group library. Writes nothing."""
    resp = requests.get("https://api.zotero.org/keys/current", timeout=TIMEOUT,
                        headers={"Zotero-API-Key": api_key,
                                 "Zotero-API-Version": "3"})
    resp.raise_for_status()
    groups = (resp.json().get("access") or {}).get("groups") or {}
    access = groups.get(str(group)) or groups.get("all") or {}
    return bool(access.get("library") and access.get("write"))


def describe(ts):
    """Where to find a #papers post, without quoting it: its ts and UTC date."""
    try:
        posted = datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc)
        return f"post {ts} ({posted:%Y-%m-%d %H:%M} UTC)"
    except (TypeError, ValueError):
        return f"post {ts}"


def main(slack_token, channel_id, zotero_group, zotero_api_key, collection,
         messages, dry_run=False, now=None):
    """Promote every nominated paper and return a summary dict.

    messages is zotbot.json's {Slack ts: Zotero item key} map. A failure on
    one paper is logged and does not stop the others. Logs name posts by ts
    and papers by Zotero key only, never by their text: Actions logs are
    public.
    """
    summary = {"scanned": 0, "nominated": 0, "resolved": 0, "already": 0,
               "promoted": [], "unresolved": [], "failures": 0}
    seen = set()

    if dry_run:
        # A dry run never reaches the PATCH, so check the key can make it.
        try:
            writable = can_write(zotero_group, zotero_api_key)
        except Exception as e:
            writable = False
            print(f"Could not check Zotero key access: {type(e).__name__}")
        print(f"Zotero key can write to the group library: "
              f"{'yes' if writable else 'NO'}")
        if not writable:
            summary["failures"] += 1

    for message in history(slack_token, channel_id, now=now):
        summary["scanned"] += 1
        if votes(message) < THRESHOLD or not is_zotbot_post(message, messages):
            continue
        summary["nominated"] += 1
        ts = message["ts"]
        where = describe(ts)

        try:
            key, how = resolve(message, messages, zotero_group, zotero_api_key)
        except Unresolved as e:
            summary["unresolved"].append(ts)
            print(f"UNRESOLVED {where}: {e}")
            continue
        except Exception as e:
            # Not str(e): an HTTP error quotes the search URL, title included.
            status = getattr(getattr(e, "response", None), "status_code", "")
            summary["failures"] += 1
            print(f"Error resolving {where}: {type(e).__name__} {status}".rstrip())
            continue
        summary["resolved"] += 1

        if key in seen:
            print(f"{key} ({where}, via {how}): already handled in this run")
            continue
        seen.add(key)

        try:
            if not add_to_collection(zotero_group, zotero_api_key, key,
                                     collection, dry_run=dry_run):
                summary["already"] += 1
                print(f"{key} ({where}, via {how}): already in Journal Club")
                continue
            summary["promoted"].append(key)
            if dry_run:
                print(f"[DRY RUN] {key} ({where}, via {how}): would promote")
                continue
            print(f"{key} ({where}, via {how}): promoted to Journal Club")
            slack("chat.postMessage", slack_token, http_method="post",
                  channel=channel_id, thread_ts=ts,
                  text=confirmation(voters(message)))
        except Exception as e:
            summary["failures"] += 1
            print(f"Error promoting {key} ({where}): {type(e).__name__}: {e}")

    print("\n".join([
        "",
        f"Slack messages scanned: {summary['scanned']}",
        f"Nominated ZotBot posts: {summary['nominated']}",
        f"Resolved Zotero items: {summary['resolved']}",
        f"Already in Journal Club: {summary['already']}",
        f"Newly promoted: {len(summary['promoted'])}"
        + (" (dry run, nothing written)" if dry_run else ""),
        f"Unresolved nominations: {len(summary['unresolved'])}",
        f"Failures: {summary['failures']}",
    ]))
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Read Slack and Zotero, but write nothing")
    parser.add_argument("--artifact", default="zotbot.json",
                        help="ZotBot artifact holding the message map")
    args = parser.parse_args()

    names = ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "ZOTERO_GROUP",
             "ZOTERO_WRITE_API_KEY", "ZOTERO_JOURNAL_CLUB_COLLECTION")
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        parser.error("missing environment: " + ", ".join(missing))

    summary = main(*(os.environ[n] for n in names),
                   messages=load_messages(args.artifact),
                   dry_run=args.dry_run)
    if summary["failures"] or summary["unresolved"]:
        sys.exit(1)
