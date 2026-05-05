"""
Orchestration scheduler.
Market hours: monitor every 30 min (9:30am-4pm ET weekdays)
After hours: 4:30pm EOD summary, 8am pre-market check.
Scanner: full thesis + opportunity scan every 2 hours during market hours,
         once at EOD.

Run: python scheduler.py
"""

import logging
import os
import sys
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()

from monitor import run_all_checks
from content_engine import generate_content
from draft_queue import queue_draft
from market_scanner import run_full_scan, ScanResult
from decision_logger import log
from security import startup_check
from sms_notifier import send_alert, send_digest, send_rule_breach

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def _is_market_hours() -> bool:
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close


def monitor_and_queue(label: str = "scheduled"):
    logger.info("monitor cycle starting [%s]", label)
    log.decision(
        component="scheduler",
        action="monitor_cycle",
        reasoning=f"Triggered monitor cycle: {label}",
        outcome="starting",
    )
    try:
        events = run_all_checks()
        logger.info("%d events detected", len(events))

        queued = 0
        for event in events:
            try:
                content = generate_content(event)
                from security import seal_draft
                path = queue_draft(event, content)
                seal_draft(path)
                logger.info("queued + sealed: %s", path.name)
                queued += 1

                # SMS alert for action_required events
                if event.severity == "action_required":
                    if event.event_type == "rule_alert":
                        send_rule_breach(
                            ticker=event.ticker,
                            rule=event.data.get("rule", "rule violation"),
                            detail=f"{event.data.get('pct_of_book', '?')}% of spec book",
                        )
                    else:
                        send_alert(
                            message=f"{event.event_type}: {event.data}",
                            priority="high",
                            ticker=event.ticker,
                        )
            except Exception as e:
                logger.error("failed to process event %s/%s: %s", event.ticker, event.event_type, e)
                log.decision(
                    component="scheduler",
                    action="event_processing",
                    reasoning=f"Failed to process {event.ticker}/{event.event_type}: {e}",
                    outcome="error",
                    severity="flag",
                )

        log.decision(
            component="scheduler",
            action="monitor_cycle",
            reasoning=f"Cycle {label} complete: {queued}/{len(events)} events queued as drafts",
            outcome="complete",
            data={"label": label, "events": len(events), "queued": queued},
        )
        logger.info("monitor cycle complete — %d/%d events queued", queued, len(events))
    except Exception as e:
        logger.error("monitor cycle failed: %s", e, exc_info=True)
        log.decision(
            component="scheduler",
            action="monitor_cycle",
            reasoning=f"Cycle {label} failed with unhandled exception: {e}",
            outcome="failed",
            severity="action_required",
        )


def scanner_job(label: str = "scheduled"):
    """Run full market scan — thesis evaluation, peers, drift, opportunities."""
    logger.info("scanner cycle starting [%s]", label)
    log.decision(
        component="scheduler",
        action="scanner_cycle",
        reasoning=f"Triggered scanner cycle: {label}",
        outcome="starting",
    )
    try:
        results = run_full_scan()
        critical = [r for r in results if r.priority == "critical"]
        high = [r for r in results if r.priority == "high"]

        logger.info(
            "scanner complete: %d total findings (%d critical, %d high)",
            len(results), len(critical), len(high),
        )

        # Write high/critical findings as internal draft files for review
        for result in results:
            if result.priority in ("critical", "high"):
                _queue_scan_result(result)
                # SMS alert for critical scanner findings
                if result.priority == "critical":
                    send_alert(
                        message=f"{result.scan_type}: {result.finding}\n{result.reasoning[:200]}",
                        priority="critical",
                        ticker=result.ticker,
                    )

        log.decision(
            component="scheduler",
            action="scanner_cycle",
            reasoning=f"Scanner {label} complete: {len(results)} findings, {len(critical)} critical, {len(high)} high priority queued for review",
            outcome="complete",
            data={
                "total": len(results),
                "critical": len(critical),
                "high": len(high),
                "label": label,
            },
        )
    except Exception as e:
        logger.error("scanner cycle failed: %s", e, exc_info=True)
        log.decision(
            component="scheduler",
            action="scanner_cycle",
            reasoning=f"Scanner {label} failed: {e}",
            outcome="failed",
            severity="action_required",
        )


def _queue_scan_result(result: ScanResult):
    """Convert a ScanResult into a draft file for review."""
    import json
    import re
    from pathlib import Path
    from datetime import datetime, timezone

    drafts_dir = Path(__file__).parent / "drafts"
    drafts_dir.mkdir(exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    safe_ticker = re.sub(r"[^A-Z0-9]", "", result.ticker.upper())
    path = drafts_dir / f"{ts}_{safe_ticker}_{result.scan_type}.json"

    draft = {
        "status": "pending",
        "source": "scanner",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scan_result": {
            "ticker": result.ticker,
            "scan_type": result.scan_type,
            "priority": result.priority,
            "finding": result.finding,
            "reasoning": result.reasoning,
            "data": result.data,
            "timestamp": result.timestamp.isoformat(),
        },
        "content": {
            "content_type": "internal",
            "content": f"[{result.priority.upper()}] {result.ticker} — {result.finding}\n\n{result.reasoning}",
            "bedrock_used": False,
        },
    }

    with open(path, "w") as f:
        json.dump(draft, f, indent=2)
    logger.info("scan result queued: %s", path.name)


def intraday_job():
    """Runs every 30 min during market hours."""
    if _is_market_hours():
        monitor_and_queue("intraday")
    else:
        logger.debug("outside market hours — skipping intraday run")


def intraday_scan_job():
    """Deep scan every 2 hours during market hours."""
    if _is_market_hours():
        scanner_job("intraday")
    else:
        logger.debug("outside market hours — skipping intraday scan")


def eod_job():
    """4:30pm ET — end-of-day monitor + full scan + SMS digest."""
    logger.info("running EOD summary")
    monitor_and_queue("eod")

    scan_results = []
    try:
        scan_results = run_full_scan()
        for result in scan_results:
            if result.priority in ("critical", "high"):
                _queue_scan_result(result)
    except Exception as e:
        logger.error("EOD scanner failed: %s", e)

    # Count queued drafts for digest
    from draft_queue import list_pending_drafts
    pending_count = len(list_pending_drafts())

    # Load today's events count from decision log
    from status import _load_recent_decisions
    records = _load_recent_decisions(hours=8)
    event_count = sum(1 for r in records if r.get("action") == "event_created")

    send_digest(scan_results=scan_results, events=list(range(event_count)), queued_drafts=pending_count)


def premarket_job():
    """8:00am ET pre-market catalyst check + scan."""
    logger.info("running pre-market check")
    monitor_and_queue("premarket")
    scanner_job("premarket")


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=ET)

    # Every 30 min on weekdays (guard inside job checks market hours)
    scheduler.add_job(
        intraday_job,
        CronTrigger(day_of_week="mon-fri", hour="9-16", minute="0,30", timezone=ET),
        id="intraday",
        name="Intraday monitor (30 min)",
        replace_existing=True,
    )

    # Deep scan every 2 hours during market hours
    scheduler.add_job(
        intraday_scan_job,
        CronTrigger(day_of_week="mon-fri", hour="10,12,14,16", minute=0, timezone=ET),
        id="intraday_scan",
        name="Intraday scanner (2 hr)",
        replace_existing=True,
    )

    # EOD summary + scan at 4:30pm ET weekdays
    scheduler.add_job(
        eod_job,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone=ET),
        id="eod",
        name="EOD summary + scan",
        replace_existing=True,
    )

    # Pre-market at 8:00am ET weekdays
    scheduler.add_job(
        premarket_job,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=ET),
        id="premarket",
        name="Pre-market check + scan",
        replace_existing=True,
    )

    return scheduler


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("scheduler.log"),
        ],
    )

    logger.info("Portfolio Intelligence Agent starting")
    logger.info("Approval mode: %s", os.getenv("APPROVAL_MODE", "file"))

    # Security gate — halts on missing critical credentials
    startup_check(halt_on_critical=True)

    log.decision(
        component="scheduler",
        action="startup",
        reasoning="Agent starting — security check passed, running immediate monitor + scanner cycle",
        outcome="initializing",
        data={"approval_mode": os.getenv("APPROVAL_MODE", "file")},
    )

    scheduler = build_scheduler()

    # Immediate cycles on startup
    logger.info("running initial monitor cycle on startup")
    monitor_and_queue("startup")
    logger.info("running initial scanner cycle on startup")
    scanner_job("startup")

    logger.info("scheduler started — next jobs:")
    for job in scheduler.get_jobs():
        logger.info("  %s: %s", job.name, job.next_run_time)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler stopped")
        log.decision(
            component="scheduler",
            action="shutdown",
            reasoning="KeyboardInterrupt or SystemExit received — agent stopping cleanly",
            outcome="stopped",
        )


if __name__ == "__main__":
    main()
