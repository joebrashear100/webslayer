"""
Manual posting script — intentionally NOT automated.
Run this script to post approved drafts to Twitter.
Requires human to have reviewed and approved drafts first.

Usage:
    python post_approved.py              # posts all approved drafts
    python post_approved.py --dry-run    # previews without posting
    python post_approved.py --file drafts/2026-05-05_10-00_MNKD_price_move.json
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import tweepy
from dotenv import load_dotenv

load_dotenv()

from draft_queue import list_approved_drafts, update_status

logger = logging.getLogger(__name__)


def _get_twitter_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.getenv("TWITTER_API_KEY"),
        consumer_secret=os.getenv("TWITTER_API_SECRET"),
        access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
        access_token_secret=os.getenv("TWITTER_ACCESS_SECRET"),
    )


def _post_content(client: tweepy.Client, content: str, content_type: str, dry_run: bool) -> list[str]:
    """Returns list of posted tweet IDs (or mock IDs in dry-run)."""
    posted_ids = []

    if content_type == "thread":
        tweets = _parse_thread(content)
        prev_id = None
        for i, tweet_text in enumerate(tweets):
            if dry_run:
                print(f"  [DRY RUN] Tweet {i+1}/{len(tweets)}: {tweet_text[:100]}...")
                posted_ids.append(f"dry-run-{i}")
            else:
                kwargs = {"text": tweet_text}
                if prev_id:
                    kwargs["in_reply_to_tweet_id"] = prev_id
                resp = client.create_tweet(**kwargs)
                prev_id = resp.data["id"]
                posted_ids.append(prev_id)
                logger.info("posted tweet %s", prev_id)
    else:
        if dry_run:
            print(f"  [DRY RUN] Tweet: {content[:100]}...")
            posted_ids.append("dry-run-0")
        else:
            resp = client.create_tweet(text=content[:280])
            posted_ids.append(resp.data["id"])
            logger.info("posted tweet %s", resp.data["id"])

    return posted_ids


def _parse_thread(content: str) -> list[str]:
    """Split thread content on tweet markers (1/, 2/, etc.)."""
    import re
    parts = re.split(r'(?m)^(\d+/)', content)
    tweets = []
    i = 1
    while i < len(parts):
        marker = parts[i]
        text = parts[i + 1].strip() if i + 1 < len(parts) else ""
        tweets.append((marker + text).strip())
        i += 2
    return tweets if tweets else [content.strip()]


def post_draft(path: Path, dry_run: bool = False) -> bool:
    with open(path) as f:
        draft = json.load(f)

    if draft["status"] != "approved":
        logger.warning("skipping %s — status is '%s', not approved", path.name, draft["status"])
        return False

    content = draft["content"].get("content", "")
    content_type = draft["content"].get("content_type", "tweet")
    ticker = draft["event"]["ticker"]

    print(f"\nPosting: {path.name}")
    print(f"  Ticker: {ticker} | Type: {content_type}")
    print(f"  Content preview: {content[:120]}...")

    if not dry_run:
        confirm = input("  Confirm post? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            return False

    try:
        client = _get_twitter_client()
        ids = _post_content(client, content, content_type, dry_run)
        if not dry_run:
            update_status(path, "posted")
            print(f"  Posted. Tweet IDs: {ids}")
        return True
    except Exception as e:
        logger.error("failed to post %s: %s", path.name, e)
        print(f"  ERROR: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Post approved Twitter drafts")
    parser.add_argument("--dry-run", action="store_true", help="Preview without posting")
    parser.add_argument("--file", type=Path, help="Post a specific draft file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.file:
        targets = [args.file]
    else:
        targets = list_approved_drafts()

    if not targets:
        print("No approved drafts found.")
        sys.exit(0)

    print(f"Found {len(targets)} approved draft(s).")
    posted = 0
    for path in targets:
        if post_draft(path, dry_run=args.dry_run):
            posted += 1

    print(f"\nDone. Posted {posted}/{len(targets)}.")


if __name__ == "__main__":
    main()
