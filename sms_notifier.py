"""
Twilio SMS notification layer.

Features:
- Kill switch: set SMS_KILL_SWITCH=true to silence all messages
- Rate limit: max SMS_RATE_LIMIT_PER_HOUR (default 10) per hour, tracked in
  .sms_rate_state.json — prevents runaway loops from text-bombing you
- Severity gate: only sends for priority high/critical by default
- Daily digest: structured EOD summary of scan findings + queued drafts
- Circuit breaker: after 3 consecutive Twilio failures, pauses for 1 hour

Required env vars:
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER

Optional:
  SMS_KILL_SWITCH=true          — silences all outbound messages
  SMS_RATE_LIMIT_PER_HOUR=10   — max texts per hour (default 10)
  SMS_MIN_PRIORITY=high         — minimum priority to send (high|critical)
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from decision_logger import log

logger = logging.getLogger(__name__)

_RATE_STATE_PATH = Path(__file__).parent / ".sms_rate_state.json"
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN_MINUTES = 60


def _load_rate_state() -> dict:
    if _RATE_STATE_PATH.exists():
        try:
            with open(_RATE_STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"sent_timestamps": [], "consecutive_failures": 0, "circuit_open_until": None}


def _save_rate_state(state: dict) -> None:
    with open(_RATE_STATE_PATH, "w") as f:
        json.dump(state, f, default=str)


def _is_kill_switch_active() -> bool:
    return os.getenv("SMS_KILL_SWITCH", "").lower() in ("true", "1", "yes")


def _rate_limit_per_hour() -> int:
    try:
        return int(os.getenv("SMS_RATE_LIMIT_PER_HOUR", "10"))
    except ValueError:
        return 10


def _check_rate_limit() -> tuple[bool, str]:
    """Returns (allowed, reason). Prunes old timestamps, checks circuit breaker."""
    state = _load_rate_state()
    now = datetime.now(timezone.utc)

    # Circuit breaker check
    if state.get("circuit_open_until"):
        try:
            open_until = datetime.fromisoformat(state["circuit_open_until"])
            if now < open_until:
                remaining = int((open_until - now).total_seconds() / 60)
                return False, f"circuit breaker open — {remaining}m remaining after {_CIRCUIT_BREAKER_THRESHOLD} consecutive failures"
            else:
                state["consecutive_failures"] = 0
                state["circuit_open_until"] = None
        except (ValueError, TypeError):
            pass

    # Prune timestamps older than 1 hour
    cutoff = now - timedelta(hours=1)
    state["sent_timestamps"] = [
        ts for ts in state.get("sent_timestamps", [])
        if datetime.fromisoformat(ts) > cutoff
    ]

    limit = _rate_limit_per_hour()
    count = len(state["sent_timestamps"])
    if count >= limit:
        return False, f"rate limit reached ({count}/{limit} per hour)"

    _save_rate_state(state)
    return True, f"{count}/{limit} sent this hour"


def _record_send(success: bool) -> None:
    state = _load_rate_state()
    now = datetime.now(timezone.utc)

    if success:
        state.setdefault("sent_timestamps", []).append(now.isoformat())
        state["consecutive_failures"] = 0
        state["circuit_open_until"] = None
    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        if state["consecutive_failures"] >= _CIRCUIT_BREAKER_THRESHOLD:
            open_until = now + timedelta(minutes=_CIRCUIT_BREAKER_COOLDOWN_MINUTES)
            state["circuit_open_until"] = open_until.isoformat()
            logger.warning(
                "SMS circuit breaker opened — pausing for %d minutes after %d failures",
                _CIRCUIT_BREAKER_COOLDOWN_MINUTES,
                _CIRCUIT_BREAKER_THRESHOLD,
            )

    _save_rate_state(state)


def _get_twilio_client():
    from twilio.rest import Client
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise EnvironmentError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return Client(sid, token)


def _send_raw(body: str) -> bool:
    """Send a raw SMS. Returns True on success."""
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number = os.getenv("TWILIO_TO_NUMBER")
    if not from_number or not to_number:
        raise EnvironmentError("TWILIO_FROM_NUMBER and TWILIO_TO_NUMBER must be set")

    client = _get_twilio_client()
    msg = client.messages.create(body=body[:1600], from_=from_number, to=to_number)
    logger.info("SMS sent: SID=%s status=%s", msg.sid, msg.status)
    return True


def send_alert(message: str, priority: str = "high", ticker: str = "") -> bool:
    """
    Send a priority SMS alert.
    Only sends for priority >= SMS_MIN_PRIORITY (default: high).
    Returns True if sent, False if suppressed or failed.
    """
    min_priority = os.getenv("SMS_MIN_PRIORITY", "high").lower()
    priority_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    if priority_order.get(priority, 0) < priority_order.get(min_priority, 2):
        logger.debug("SMS suppressed — priority %s below threshold %s", priority, min_priority)
        return False

    if _is_kill_switch_active():
        logger.info("SMS kill switch active — suppressing: %s", message[:80])
        log.decision(
            component="sms",
            action="send_alert",
            reasoning=f"Kill switch active — alert suppressed: {message[:80]}",
            outcome="suppressed",
            data={"priority": priority, "ticker": ticker},
        )
        return False

    allowed, reason = _check_rate_limit()
    if not allowed:
        logger.warning("SMS rate limited (%s) — dropping alert: %s", reason, message[:80])
        log.decision(
            component="sms",
            action="send_alert",
            reasoning=f"Rate limited ({reason}) — alert dropped",
            outcome="rate_limited",
            data={"priority": priority, "ticker": ticker, "message_preview": message[:80]},
            severity="flag",
        )
        return False

    prefix = f"[{priority.upper()}]" + (f" {ticker}" if ticker else "")
    body = f"{prefix}\n{message}"

    try:
        _send_raw(body)
        _record_send(True)
        log.decision(
            component="sms",
            action="send_alert",
            reasoning=f"Alert sent for {ticker or 'system'} at priority {priority}",
            outcome="sent",
            data={"priority": priority, "ticker": ticker, "chars": len(body)},
        )
        return True
    except Exception as e:
        _record_send(False)
        logger.error("SMS send failed: %s", e)
        log.decision(
            component="sms",
            action="send_alert",
            reasoning=f"Twilio send failed: {e}",
            outcome="failed",
            data={"error": str(e), "ticker": ticker},
            severity="flag",
        )
        return False


def send_digest(scan_results: list, events: list, queued_drafts: int) -> bool:
    """
    Send an EOD digest summarizing scan findings and queued drafts.
    Always attempts to send regardless of SMS_MIN_PRIORITY, but still
    respects kill switch and rate limit.
    """
    if _is_kill_switch_active():
        logger.info("SMS kill switch active — suppressing digest")
        return False

    allowed, reason = _check_rate_limit()
    if not allowed:
        logger.warning("SMS rate limited (%s) — dropping digest", reason)
        return False

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    critical = [r for r in scan_results if r.priority == "critical"]
    high = [r for r in scan_results if r.priority == "high"]

    lines = [
        f"Portfolio Digest — {now}",
        f"Events: {len(events)} | Drafts queued: {queued_drafts}",
        f"Scanner: {len(scan_results)} findings ({len(critical)} critical, {len(high)} high)",
    ]

    if critical:
        lines.append("\nCRITICAL:")
        for r in critical[:3]:
            lines.append(f"  {r.ticker}: {r.finding[:60]}")

    if high:
        lines.append("\nHIGH:")
        for r in high[:3]:
            lines.append(f"  {r.ticker}: {r.finding[:60]}")

    if not critical and not high:
        lines.append("\nAll positions nominal.")

    lines.append("\nReview: check drafts/ folder")

    body = "\n".join(lines)

    try:
        _send_raw(body)
        _record_send(True)
        log.decision(
            component="sms",
            action="send_digest",
            reasoning=f"EOD digest sent: {len(scan_results)} findings, {queued_drafts} drafts",
            outcome="sent",
        )
        return True
    except Exception as e:
        _record_send(False)
        logger.error("SMS digest failed: %s", e)
        return False


def send_rule_breach(ticker: str, rule: str, detail: str) -> bool:
    """Immediate alert for hard rule violations — bypasses priority filter."""
    message = f"RULE BREACH\n{rule}\n{ticker}: {detail}"
    return send_alert(message, priority="critical", ticker=ticker)


def get_rate_status() -> dict:
    """Return current rate limiter state for status checks."""
    state = _load_rate_state()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    recent = [
        ts for ts in state.get("sent_timestamps", [])
        if datetime.fromisoformat(ts) > cutoff
    ]
    return {
        "sent_last_hour": len(recent),
        "limit_per_hour": _rate_limit_per_hour(),
        "consecutive_failures": state.get("consecutive_failures", 0),
        "circuit_open_until": state.get("circuit_open_until"),
        "kill_switch_active": _is_kill_switch_active(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    status = get_rate_status()
    print("SMS Rate Status:")
    for k, v in status.items():
        print(f"  {k}: {v}")
