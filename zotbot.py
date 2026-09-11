#!/usr/bin/env python

"""
Simple interface between Slack Webhooks and Zotero API.
Runs periodically (e.g. via cron), fetches items since the last version,
then only alerts on items whose dateAdded is later than the last run.
"""

import os
import sys
import time
import json
import urllib.request
import datetime

import requests
from dateutil.parser import isoparse


def retrieve_articles(group_id, collection_id, api_key, limit=1, include='data', since=0):
    """Retrieves articles from a Zotero group API feed"""
    zotero_template = (
        "https://api.zotero.org/groups/{group_id}"
        "/collections/{collection_id}/items/top"
        "?start=0&limit={limit}&format=json&v=3&key={api_key}"
    )
    zotero_url = zotero_template.format(
        group_id=group_id, collection_id=collection_id,
        api_key=api_key, limit=limit
    )
    if include:
        zotero_url += f"&include={include}"
    if since:
        zotero_url += f"&since={since}"

    print(f"Retrieving most recent {limit} articles since version {since}")
    resp = urllib.request.urlopen(zotero_url)
    body = resp.readall().decode('utf-8') if hasattr(resp, 'readall') else resp.read().decode('utf-8')
    articles = json.loads(body)
    print(f"Retrieved {len(articles)} articles")
    return articles


# --- Optional OpenAI enrichment --------------------------------------------

ENRICHMENT_MODEL = 'gpt-5.6'
ENRICHMENT_TIMEOUT = 30
MAX_MENTIONS = 2

ENRICHMENT_INSTRUCTIONS = """\
You help the Bionic Vision Lab triage new papers in its Slack #papers channel.

Write exactly one concise sentence, no more than about 40 words, explaining why
this paper deserves our attention.

Write as an internal note from the lab to its own members. Use first-person
plural when referring to our research: "our work", "our models", "our experiments".
Never refer to us as "the lab", "the lab's", or "Bionic Vision Lab's".

Do not merely summarize the abstract. Identify a concrete scientific connection,
useful method, important result, conflicting result, assumption worth scrutinizing,
or implication for ongoing work. Base claims only on the supplied paper
title/abstract/tags and member research descriptions. Do not invent projects or
paper findings.

Do not mention selected members by name in the sentence. Member routing is handled
separately through Slack mentions.

Select at most two lab members with a clear direct reason to read the paper.
Prefer one when one person is clearly the strongest match. Do not add secondary
connections merely to justify more mentions. Zero is preferable to a weak match.

If the supplied information does not justify a useful sentence, return an empty
lab_context and no mention_ids."""

ENRICHMENT_SCHEMA = {
    'type': 'object',
    'properties': {
        'lab_context': {
            'type': 'string',
            'description': "One sentence on why this paper matters to the lab, "
                           "or empty if there is nothing useful to say.",
        },
        'mention_ids': {
            'type': 'array',
            'items': {'type': 'string'},
            'maxItems': MAX_MENTIONS,
            'description': "At most two slack_id values, copied verbatim from "
                           "the supplied roster.",
        },
    },
    'required': ['lab_context', 'mention_ids'],
    'additionalProperties': False,
}


def load_lab_members(raw=None):
    """Parse ZOTBOT_LAB_MEMBERS into {name, slack_id, research} dicts.

    Returns [] if the secret is absent, empty or unusable. Never logs the roster.
    """
    if raw is None:
        raw = os.environ.get('ZOTBOT_LAB_MEMBERS', '')
    if not raw or not raw.strip():
        return []

    try:
        members = json.loads(raw)
        if not isinstance(members, list):
            raise ValueError('roster is not a list')
        roster = []
        for member in members:
            if not isinstance(member, dict):
                raise ValueError('roster entry is not an object')
            slack_id = str(member.get('slack_id', '')).strip()
            research = str(member.get('research', '')).strip()
            if not slack_id or not research:
                raise ValueError('roster entry misses slack_id or research')
            roster.append({
                'name': str(member.get('name', '')).strip(),
                'slack_id': slack_id,
                'research': research,
            })
    except Exception:
        print("ZotBot enrichment disabled: invalid lab-member configuration")
        return []

    if not roster:
        return []
    return roster


def _sanitize_context(text):
    """Collapse model prose to one line of inert Slack text.

    Escaping &, < and > stops the sentence from becoming a mention, link or
    @channel broadcast; Slack renders the entities literally.
    """
    one_line = " ".join(str(text).split())
    return one_line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _clean_enrichment(result, lab_members):
    """Validate a model result into {'lab_context', 'mention_ids'} or None."""
    if not isinstance(result, dict):
        return None

    context = _sanitize_context(result.get('lab_context') or '')
    if not context:
        # No useful context means no mentions either.
        return None

    known = {member['slack_id'] for member in lab_members}
    mention_ids = []
    for candidate in result.get('mention_ids') or []:
        slack_id = candidate.strip() if isinstance(candidate, str) else ''
        if slack_id in known and slack_id not in mention_ids:
            mention_ids.append(slack_id)
        if len(mention_ids) >= MAX_MENTIONS:
            break

    return {'lab_context': context, 'mention_ids': mention_ids}


def enrich_article(article, lab_members, api_key=None, client=None):
    """Ask OpenAI why a paper matters to the lab and who should read it.

    Returns {'lab_context': str, 'mention_ids': [slack_id, ...]} on success and
    None on any problem: no abstract, no roster, no API key, API error, refusal
    or an unusable result. One attempt per paper, no retries.
    """
    data = article.get('data') or {}
    abstract = str(data.get('abstractNote', '')).strip()
    if not abstract or not lab_members:
        return None

    if api_key is None:
        api_key = os.environ.get('OPENAI_API_KEY', '')
    if not api_key and client is None:
        return None

    key = data.get('key', '?')
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, timeout=ENRICHMENT_TIMEOUT)

        paper = {
            'title': str(data.get('title', '')).strip(),
            'abstract': abstract,
        }
        tags = [t['tag'] for t in data.get('tags', []) if t.get('tag')]
        if tags:
            paper['tags'] = tags

        response = client.responses.create(
            model=ENRICHMENT_MODEL,
            reasoning={'effort': 'low'},
            # The request carries the private roster: don't let OpenAI retain it.
            store=False,
            input=[
                {'role': 'system', 'content': ENRICHMENT_INSTRUCTIONS},
                {'role': 'user', 'content': json.dumps(
                    {'paper': paper, 'lab_members': lab_members})},
            ],
            text={'format': {
                'type': 'json_schema',
                'name': 'lab_relevance',
                'strict': True,
                'schema': ENRICHMENT_SCHEMA,
            }},
        )

        # A refusal carries no output text, so this covers it too.
        output = (getattr(response, 'output_text', '') or '').strip()
        if not output:
            print(f"No enrichment for {key}: empty model output")
            return None
        result = json.loads(output)
    except Exception as e:
        print(f"No enrichment for {key}: {type(e).__name__}")
        return None

    return _clean_enrichment(result, lab_members)


def format_article(article, enrichment=None):
    """Format a Zotero item into a Slack-friendly message"""
    data = article['data']
    meta = article['meta']

    title = data.get('title', '').strip()
    submitter = meta.get('createdByUser', {}).get('username', '')
    item_type = data.get('itemType', '')
    journal = data.get('university' if item_type == 'thesis' else 'publicationTitle', '')
    authors = meta.get('creatorSummary', '').rstrip('.')
    date = data.get('date', '')

    # Build citation
    citation = ""
    if authors:
        citation += f"{authors}. "
    if journal:
        citation += f"_{journal}_ "
    if date:
        citation += date
    citation = citation.strip()

    # Abstract snippet
    abstract = data.get('abstractNote', '').strip()
    if abstract:
        words = abstract.split()
        abstract = " ".join(words[:100]) + (" ..." if len(words) > 100 else "")

    # Link via DOI or URL
    doi = data.get('DOI', '')
    url = data.get('url', '').strip()
    link = f"https://doi.org/{doi}" if doi else url

    tags = [t['tag'] for t in data.get('tags', [])]
    tag_line = ", ".join(tags)

    tmpl = ""
    if link:
        tmpl += f"<{link}|*{title}*>\n"
    else:
        tmpl += f"*{title}*\n"
    if citation:
        tmpl += f"*Citation:* {citation}\n"
    if tag_line:
        tmpl += f"*Tags:* {tag_line}\n"
    if submitter:
        tmpl += f"*Added By:* {submitter}\n"
    if enrichment and enrichment.get('lab_context'):
        context = enrichment['lab_context']
        mentions = " ".join(f"<@{i}>" for i in enrichment.get('mention_ids', []))
        if mentions:
            context += f" {mentions}"
        tmpl += f"\n*Lab context:* {context}\n"
    if abstract:
        tmpl += f"\n*Abstract:*\n```{abstract}```"

    return tmpl


def send_article_to_slack(webhook_url, article, channel=None,
                          username=None, icon_emoji=None,
                          verbose=True, mock=False, enrichment=None):
    """Send one formatted article to Slack via incoming webhook"""
    payload = {'text': format_article(article, enrichment)}
    if channel:
        payload['channel'] = channel
    if username:
        payload['username'] = username
    if icon_emoji:
        payload['icon_emoji'] = icon_emoji

    if mock:
        print(f"[MOCK POST to Slack] {payload['text'][:60]}...")
        return None

    resp = requests.post(webhook_url, json=payload)
    if resp.status_code != 200:
        print(f"Slack API error {resp.status_code}: {resp.text}")
    if verbose:
        print(f"{article['version']} – {article['data']['title']}")
    return resp


def main(zotero_group, zotero_collection, zotero_api_key,
         slack_webhook_url, since_version=0, channel=None,
         username=None, icon_emoji=None, limit=25,
         mock=False, verbose=True, artifact=None):

    # 1) current run timestamp (UTC ISO8601)
    timestamp = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'

    # 2) load last run info (time + version)
    last_run_time = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    since = since_version
    if artifact and os.path.exists(artifact):
        try:
            prev = json.load(open(artifact))
            last_run_time = isoparse(prev.get('time'))
            since = prev.get('version', since_version)
        except Exception:
            pass

    # 3) fetch all changes since that version
    articles = retrieve_articles(
        zotero_group, zotero_collection, zotero_api_key,
        limit=(limit if since else 1), since=since
    )

    # 4) compute newest version for next run
    max_version = max([since] + [a['version'] for a in articles])

    # 5) filter out edited items: only keep those truly added after last run
    new_articles = [
        a for a in articles
        if isoparse(a['data']['dateAdded']) > last_run_time
    ]

    if verbose:
        filtered = len(articles) - len(new_articles)
        print(f"Found {len(new_articles)} new items (filtered out {filtered} edits)")

    # 6) post each new item, oldest first
    lab_members = load_lab_members()
    skipped = 0
    for art in reversed(new_articles):
        # Deliberately outside the try below: a bad enrichment must never count
        # as a skipped (lost) paper.
        try:
            enrichment = enrich_article(art, lab_members)
        except Exception as e:
            enrichment = None
            print(f"No enrichment for {art['data'].get('key', '?')}: {type(e).__name__}")
        try:
            send_article_to_slack(
                slack_webhook_url, art, channel=channel,
                username=username, icon_emoji=icon_emoji,
                verbose=verbose, mock=mock, enrichment=enrichment
            )
        except Exception as e:
            skipped += 1
            print(f"Error sending {art['data']['key']}: {e}")

    # 7) prepare run info for artifact
    run_info = {
        "time": timestamp,
        "version": max_version,
        "articles_cnt": len(new_articles),
        "skipped": skipped
    }
    return run_info


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description="Retrieve Zotero group items and post new additions to Slack"
    )
    parser.add_argument('--group',      type=int,   required=True,  help='Zotero group ID')
    parser.add_argument('--collection', type=str,   required=True,  help='Zotero collection ID')
    parser.add_argument('--api',        type=str,   required=True,  help='Zotero API key')
    parser.add_argument('--webhook',    type=str,   required=True,  help='Slack webhook URL')
    parser.add_argument('--since',      type=int,   default=0,       help='Zotero version to start from')
    parser.add_argument('--limit',      type=int,   default=25,      help='Max items to fetch')
    parser.add_argument('--channel',    type=str,   default=None,    help='Slack channel override')
    parser.add_argument('--username',   type=str,   default=None,    help='Slack bot username')
    parser.add_argument('--icon',       type=str,   default=None,    help='Slack bot icon emoji')
    parser.add_argument('--artifact',   type=str,   default=None,    help='Path to JSON artifact file')
    parser.add_argument('--mock',       action='store_true',      help='Run in mock mode (no Slack writes)')
    parser.add_argument('-v',           dest='verbose', action='store_true', help='Verbose logging')
    parser.add_argument('--test',       type=str,   default=None,    help='Test file (JSON)')

    args = parser.parse_args()

    # Monkey‐patch for --test
    if args.test:
        args.mock = True
        try:
            test_articles = json.load(open(args.test))
        except Exception:
            print("Error reading test file")
            test_articles = []
        def retrieve_articles(*_a, **_k):
            return test_articles
        def send_article_to_slack(_u, art, enrichment=None, **_k):
            print(format_article(art, enrichment))
            print("-" * 40)
        # inject our mocks
        globals()['retrieve_articles'] = retrieve_articles
        globals()['send_article_to_slack'] = send_article_to_slack

    info = main(
        args.group, args.collection, args.api, args.webhook,
        since_version=args.since, channel=args.channel,
        username=args.username, icon_emoji=args.icon,
        limit=args.limit, mock=args.mock,
        verbose=args.verbose, artifact=args.artifact
    )

    # write out updated artifact, but only when the Zotero version actually
    # moved. On an idle run every field except "time" is unchanged, and
    # rewriting the file just to bump the clock produces an empty commit.
    #
    # Leaving "time" stale is safe: it is only read back as last_run_time to
    # filter out edits of already-posted items, and Zotero only returns items
    # once "version" has advanced. So a run that reads "time" is always a run
    # that also rewrote it alongside a new "version".
    if not args.mock and not args.test and args.artifact:
        prev_version = None
        if os.path.exists(args.artifact):
            try:
                prev_version = json.load(open(args.artifact)).get('version')
            except Exception:
                pass

        if info['version'] == prev_version:
            print(f"No new Zotero items (version {info['version']}); "
                  f"leaving {args.artifact} untouched")
        else:
            with open(args.artifact, 'w') as f:
                json.dump(info, f)
            print(f"Wrote run info to {args.artifact}")

    if info['skipped']:
        sys.exit(2)