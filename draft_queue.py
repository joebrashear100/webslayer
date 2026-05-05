"""
Approval queue writer.
Writes draft JSON files to drafts/ and optionally notifies via Slack.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from monitor import Event

logger = logging.getLogger(__name__)

DRAFTS_DIR = Path(__file__).parent / "drafts"
DRAFTS_DIR.mkdir(exist_ok=True)

VALID_STATUSES = {"pending", "approved", "rejected", "posted"}


def _draft_filename(event: Event) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    safe_ticker = re.sub(r"[^A-Z0-9]", "", event.ticker.upper())
    return DRAFTS_DIR / f"{ts}_{safe_ticker}_{event.event_type}.json"


def write_draft(event: Event, content_result: dict) -> Path:
    draft = {
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "event": {
            "ticker": event.ticker,
            "event_type": event.event_type,
            "severity": event.severity,
            "data": event.data,
            "timestamp": event.timestamp.isoformat(),
        },
        "content": content_result,
    }

    path = _draft_filename(event)
    with open(path, "w") as f:
        json.dump(draft, f, indent=2)

    logger.info("draft written: %s", path.name)
    return path


def _slack_blocks(draft_path: Path, draft: dict) -> list:
    content = draft["content"]
    ticker = draft["event"]["ticker"]
    event_type = draft["event"]["event_type"]
    tweet_text = content.get("content", "")

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Draft: {ticker} — {event_type}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Content ({content.get('content_type', 'tweet')}):*\n```{tweet_text}```"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity:* {draft['event']['severity']}"},
                {"type": "mrkdwn", "text": f"*Bedrock:* {'yes' if content.get('bedrock_used') else 'stub'}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"File: `{draft_path.name}`\nTo approve: edit status → `approved`, then run `python post_approved.py`"}],
        },
    ]


def notify_slack(draft_path: Path, draft: dict) -> bool:
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack notification")
        return False

    payload = {
        "text": f"New draft: {draft['event']['ticker']} / {draft['event']['event_type']}",
        "blocks": _slack_blocks(draft_path, draft),
    }

    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("slack notification sent for %s", draft_path.name)
        return True
    except Exception as e:
        logger.error("slack notification failed: %s", e)
        return False


def queue_draft(event: Event, content_result: dict) -> Path:
    path = write_draft(event, content_result)
    approval_mode = os.getenv("APPROVAL_MODE", "file").lower()

    if approval_mode == "slack":
        with open(path) as f:
            draft = json.load(f)
        notify_slack(path, draft)

    return path


def list_pending_drafts() -> list[Path]:
    return sorted(
        (p for p in DRAFTS_DIR.glob("*.json") if _read_status(p) == "pending"),
        key=lambda p: p.stat().st_mtime,
    )


def list_approved_drafts() -> list[Path]:
    return sorted(
        (p for p in DRAFTS_DIR.glob("*.json") if _read_status(p) == "approved"),
        key=lambda p: p.stat().st_mtime,
    )


def _read_status(path: Path) -> str:
    try:
        with open(path) as f:
            return json.load(f).get("status", "unknown")
    except Exception:
        return "unknown"


def update_status(path: Path, status: str) -> None:
    assert status in VALID_STATUSES, f"invalid status: {status}"
    with open(path) as f:
        draft = json.load(f)
    draft["status"] = status
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w") as f:
        json.dump(draft, f, indent=2)
    logger.info("draft %s → %s", path.name, status)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pending = list_pending_drafts()
    print(f"{len(pending)} pending drafts:")
    for p in pending:
        print(f"  {p.name}")
