"""
Bedrock-powered content generation engine.
Generates Twitter drafts for detected portfolio events in the owner's voice.
Falls back to a structured stub if Bedrock is unavailable.
"""

import os
import json
import logging
from datetime import datetime, timezone

import boto3
from dotenv import load_dotenv

load_dotenv()

from monitor import Event

logger = logging.getLogger(__name__)

VOICE_PROFILE = """VOICE PROFILE — match exactly:

Analytical and direct. No hedging. No pleasantries.
State conclusions as objective facts.
Short sentences. Zero fluff.
Use contrast to structure arguments.
Vocabulary: edge, dislocation, catalyst, floor, framework, thesis, \
structural, pre-committed, binary, mispricing.

When casual: efficient, low-context shorthand. Get to the point.
Drop words that don't carry weight.
"u" instead of "you" is acceptable in casual tweets.
No emojis unless they serve a structural purpose (🧵 for threads).
Never sound like a financial advisor disclaimer.
Sound like someone who actually holds the position.

Format rules:
- Single tweets: under 280 chars, punchy opener
- Threads: hook tweet first, number each tweet (1/, 2/ etc.)
- Never start with "I think" or "In my opinion"
- Don't explain what you're about to say — just say it"""


def _build_prompt(event: Event) -> str:
    base = f"""You are generating Twitter/X content for a retail investor managing a speculative equity portfolio.

{VOICE_PROFILE}

---

EVENT:
Ticker: {event.ticker}
Type: {event.event_type}
Severity: {event.severity}
Data: {json.dumps(event.data, indent=2)}

---

"""

    if event.event_type == "price_move":
        direction = event.data.get("direction", "moved")
        pct = event.data.get("pct_change", 0)
        thesis = event.data.get("thesis", "")
        base += f"""Generate a single tweet (under 280 chars) about {event.ticker} moving {pct}% {direction} today.
Include thesis status context: "{thesis}"
State whether this changes the thesis or is noise. Be direct."""

    elif event.event_type == "news":
        headlines = event.data.get("headlines", [])
        thesis = event.data.get("thesis", "")
        count = event.data.get("article_count", 0)
        headline_str = "; ".join(headlines[:3])
        if count >= 3:
            base += f"""Generate a 3-tweet thread (1/, 2/, 3/) about {event.ticker} news.
Headlines: {headline_str}
Thesis: "{thesis}"
Tweet 1: hook with the news signal. Tweet 2: thesis impact analysis. Tweet 3: position stance."""
        else:
            base += f"""Generate a single tweet about {event.ticker} news.
Headline: {headlines[0] if headlines else "News event detected"}
Thesis: "{thesis}"
Analyze whether this is signal or noise relative to the thesis."""

    elif event.event_type == "catalyst_proximity":
        days = event.data.get("days_away", 0)
        cat_date = event.data.get("catalyst_date", "")
        cat_type = event.data.get("catalyst_type", "catalyst")
        exit_rule = event.data.get("exit_rule", "")
        thesis = event.data.get("thesis", "")
        base += f"""Generate a single tweet about {event.ticker} with {days} days until {cat_type} ({cat_date}).
Thesis: "{thesis}"
{"Exit rule: " + exit_rule if exit_rule else ""}
Include a countdown and the pre-committed plan. No waffling."""

    elif event.event_type == "entry_window":
        target = event.data.get("target_shares", "")
        pdufa = event.data.get("pdufa", "")
        thesis = event.data.get("thesis", "")
        base += f"""Generate a single tweet announcing entry into {event.ticker}.
Target: {target} shares. PDUFA: {pdufa}.
Thesis: "{thesis}"
State the binary clearly. Sound like someone with conviction who did the work."""

    else:
        base += "Generate a brief internal note about this event. No public content needed."

    base += "\n\nOutput ONLY the tweet text. No labels, no preamble, no explanation."
    return base


def _invoke_bedrock(prompt: str) -> str:
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    model_id = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6")
    client = boto3.client("bedrock-runtime", region_name=region)

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    })

    resp = client.invoke_model(modelId=model_id, body=body)
    result = json.loads(resp["body"].read())
    return result["content"][0]["text"].strip()


def _stub_content(event: Event) -> str:
    """Fallback when Bedrock is unavailable — produces a structured placeholder."""
    if event.event_type == "price_move":
        direction = event.data.get("direction", "moved")
        pct = event.data.get("pct_change", 0)
        return f"[STUB] {event.ticker} {'+' if direction == 'up' else ''}{pct}% today. Thesis: {event.data.get('thesis', 'N/A')}. Watching."
    elif event.event_type == "news":
        headlines = event.data.get("headlines", [])
        h = headlines[0] if headlines else "news event"
        return f"[STUB] {event.ticker}: {h}. Thesis intact. Will monitor."
    elif event.event_type == "catalyst_proximity":
        days = event.data.get("days_away", "?")
        return f"[STUB] {event.ticker} catalyst in {days} days. Pre-committed exit ready."
    elif event.event_type == "entry_window":
        return f"[STUB] {event.ticker} entry window open. Target {event.data.get('target_shares', '?')} shares. Binary thesis."
    return f"[STUB] {event.ticker} — {event.event_type} detected."


def generate_content(event: Event) -> dict:
    """
    Returns a content dict:
    {
        "event_type": str,
        "ticker": str,
        "content": str,          # tweet text or thread text
        "content_type": str,     # tweet | thread | internal
        "generated_at": str,
        "bedrock_used": bool,
    }
    """
    if event.event_type == "rule_alert":
        return {
            "event_type": event.event_type,
            "ticker": event.ticker,
            "content": f"INTERNAL RULE ALERT — {event.data.get('rule', 'unknown rule')} | {event.ticker} at {event.data.get('pct_of_book', '?')}% of spec book",
            "content_type": "internal",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bedrock_used": False,
        }

    prompt = _build_prompt(event)
    bedrock_used = False

    try:
        content = _invoke_bedrock(prompt)
        bedrock_used = True
        logger.info("bedrock generated content for %s/%s", event.ticker, event.event_type)
    except Exception as e:
        logger.warning("bedrock unavailable (%s) — using stub for %s/%s", e, event.ticker, event.event_type)
        content = _stub_content(event)

    is_thread = "\n1/" in content or content.startswith("1/")
    content_type = "thread" if is_thread else "tweet"
    if event.event_type == "news" and event.data.get("article_count", 0) >= 3:
        content_type = "thread"

    return {
        "event_type": event.event_type,
        "ticker": event.ticker,
        "content": content,
        "content_type": content_type,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bedrock_used": bedrock_used,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Quick smoke test with a synthetic event
    from monitor import Event
    from datetime import timezone
    test_event = Event(
        ticker="SNDK",
        event_type="price_move",
        severity="flag",
        data={
            "prev_close": 50.00,
            "curr_close": 53.10,
            "pct_change": 6.2,
            "direction": "up",
            "thesis": "NAND supercycle + HBF post-trim ride",
        },
    )
    result = generate_content(test_event)
    print(json.dumps(result, indent=2))
