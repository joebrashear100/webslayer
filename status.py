"""
Quick status reporter — run at any time to see current agent state.

Usage:
    python status.py
    python status.py --json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _load_drafts_summary() -> dict:
    drafts_dir = Path(__file__).parent / "drafts"
    if not drafts_dir.exists():
        return {"total": 0, "pending": 0, "approved": 0, "posted": 0, "rejected": 0}

    counts = {"total": 0, "pending": 0, "approved": 0, "posted": 0, "rejected": 0}
    for path in drafts_dir.glob("*.json"):
        if path.name == ".gitkeep":
            continue
        counts["total"] += 1
        try:
            with open(path) as f:
                draft = json.load(f)
            status = draft.get("status", "unknown")
            if status in counts:
                counts[status] += 1
        except (json.JSONDecodeError, OSError):
            pass
    return counts


def _load_recent_decisions(hours: int = 4) -> list[dict]:
    logs_dir = Path(__file__).parent / "logs"
    if not logs_dir.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    records = []

    for path in sorted(logs_dir.glob("decisions_*.jsonl"), reverse=True)[:2]:
        try:
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                ts_str = rec.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts >= cutoff:
                        records.append(rec)
                except ValueError:
                    pass
        except (OSError, json.JSONDecodeError):
            pass

    return sorted(records, key=lambda r: r.get("timestamp", ""), reverse=True)


def _last_cycle_time(records: list[dict]) -> str:
    for rec in records:
        if rec.get("component") == "monitor" and rec.get("action") == "cycle_complete":
            return rec["timestamp"]
    return "no recent cycle"


def _last_scan_time(records: list[dict]) -> str:
    for rec in records:
        if rec.get("component") == "scanner" and rec.get("action") == "full_scan_complete":
            return rec["timestamp"]
    return "no recent scan"


def _critical_findings(records: list[dict]) -> list[str]:
    findings = []
    for rec in records:
        if rec.get("component") in ("scanner", "monitor"):
            severity = rec.get("severity", "") or rec.get("priority", "")
            if severity in ("critical", "action_required"):
                findings.append(f"{rec.get('ticker', '')} — {rec.get('reasoning', rec.get('finding', ''))[:80]}")
    return findings[:5]


def _sms_status() -> dict:
    try:
        from sms_notifier import get_rate_status
        return get_rate_status()
    except Exception as e:
        return {"error": str(e)}


def build_report() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    drafts = _load_drafts_summary()
    records = _load_recent_decisions(hours=6)
    critical = _critical_findings(records)

    return {
        "generated_at": now,
        "drafts": drafts,
        "last_monitor_cycle": _last_cycle_time(records),
        "last_scanner_cycle": _last_scan_time(records),
        "recent_events_logged": len(records),
        "critical_findings": critical,
        "sms": _sms_status(),
        "approval_mode": os.getenv("APPROVAL_MODE", "file"),
    }


def print_report(report: dict) -> None:
    print(f"\n{'='*52}")
    print(f"  Portfolio Intelligence Agent — Status")
    print(f"  {report['generated_at']}")
    print(f"{'='*52}")

    d = report["drafts"]
    print(f"\nDrafts:")
    print(f"  Pending:  {d['pending']}")
    print(f"  Approved: {d['approved']}  (run post_approved.py to post)")
    print(f"  Posted:   {d['posted']}")
    print(f"  Rejected: {d['rejected']}")
    print(f"  Total:    {d['total']}")

    print(f"\nAgent Cycles:")
    print(f"  Last monitor:  {report['last_monitor_cycle']}")
    print(f"  Last scan:     {report['last_scanner_cycle']}")
    print(f"  Events logged (6h): {report['recent_events_logged']}")

    if report["critical_findings"]:
        print(f"\nCritical Findings:")
        for f in report["critical_findings"]:
            print(f"  ⚠  {f}")
    else:
        print(f"\nNo critical findings in recent history.")

    sms = report["sms"]
    print(f"\nSMS (Twilio):")
    if "error" in sms:
        print(f"  Status: unavailable ({sms['error']})")
    else:
        ks = " [KILL SWITCH ON]" if sms.get("kill_switch_active") else ""
        cb = f" [CIRCUIT OPEN until {sms['circuit_open_until']}]" if sms.get("circuit_open_until") else ""
        print(f"  Sent (1h): {sms.get('sent_last_hour', 0)}/{sms.get('limit_per_hour', 10)}{ks}{cb}")
        print(f"  Failures:  {sms.get('consecutive_failures', 0)}")

    print(f"\nApproval mode: {report['approval_mode']}")
    print(f"{'='*52}\n")


def main():
    parser = argparse.ArgumentParser(description="Portfolio Intelligence Agent status")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    report = build_report()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
