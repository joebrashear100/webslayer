"""
Orchestration scheduler.
Market hours: monitor every 30 min (9:30am-4pm ET weekdays)
After hours: 4:30pm EOD summary, 8am pre-market check.

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
    try:
        events = run_all_checks()
        logger.info("%d events detected", len(events))

        queued = 0
        for event in events:
            try:
                content = generate_content(event)
                path = queue_draft(event, content)
                logger.info("queued: %s", path.name)
                queued += 1
            except Exception as e:
                logger.error("failed to process event %s/%s: %s", event.ticker, event.event_type, e)

        logger.info("monitor cycle complete — %d/%d events queued", queued, len(events))
    except Exception as e:
        logger.error("monitor cycle failed: %s", e, exc_info=True)


def intraday_job():
    """Runs every 30 min during market hours."""
    if _is_market_hours():
        monitor_and_queue("intraday")
    else:
        logger.debug("outside market hours — skipping intraday run")


def eod_job():
    """4:30pm ET end-of-day summary."""
    logger.info("running EOD summary")
    monitor_and_queue("eod")


def premarket_job():
    """8:00am ET pre-market catalyst check."""
    logger.info("running pre-market check")
    monitor_and_queue("premarket")


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

    # EOD summary at 4:30pm ET weekdays
    scheduler.add_job(
        eod_job,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone=ET),
        id="eod",
        name="EOD summary",
        replace_existing=True,
    )

    # Pre-market at 8:00am ET weekdays
    scheduler.add_job(
        premarket_job,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=ET),
        id="premarket",
        name="Pre-market check",
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

    scheduler = build_scheduler()

    # Run one immediate cycle on startup
    logger.info("running initial monitor cycle on startup")
    monitor_and_queue("startup")

    logger.info("scheduler started — next jobs:")
    for job in scheduler.get_jobs():
        logger.info("  %s: %s", job.name, job.next_run_time)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
