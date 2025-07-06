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


def format_article(article):
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
    if abstract:
        tmpl += f"\n*Abstract:*\n```{abstract}```"

    return tmpl


def send_article_to_slack(webhook_url, article, channel=None,
                          username=None, icon_emoji=None,
                          verbose=True, mock=False):
    """Send one formatted article to Slack via incoming webhook"""
    payload = {'text': format_article(article)}
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
    skipped = 0
    for art in reversed(new_articles):
        try:
            send_article_to_slack(
                slack_webhook_url, art, channel=channel,
                username=username, icon_emoji=icon_emoji,
                verbose=verbose, mock=mock
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
        def send_article_to_slack(_u, art, **_k):
            print(format_article(art))
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

    # write out updated artifact
    if not args.mock and not args.test and args.artifact:
        with open(args.artifact, 'w') as f:
            json.dump(info, f)
        print(f"Wrote run info to {args.artifact}")

    if info['skipped']:
        sys.exit(2)