"""
Decision log viewer — pretty-prints agent reasoning from JSONL logs.

Usage:
    python log_viewer.py                          # today's log
    python log_viewer.py --date 2026-05-05        # specific date
    python log_viewer.py --component scanner      # filter by component
    python log_viewer.py --severity action_required
    python log_viewer.py --tail 20                # last N entries
    python log_viewer.py --summary               # stats only
"""

import argparse
import json
from datetime import datetime, timezone, date
from pathlib import Path

LOGS_DIR = Path(__file__).parent / "logs"

SEVERITY_COLORS = {
    "action_required": "\033[91m",  # red
    "flag": "\033[93m",             # yellow
    "info": "\033[0m",              # default
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

COMPONENT_COLORS = {
    "monitor": "\033[94m",      # blue
    "scanner": "\033[96m",      # cyan
    "content_engine": "\033[95m",  # magenta
    "sms": "\033[92m",          # green
    "scheduler": "\033[33m",    # dark yellow
    "evaluator": "\033[95m",    # magenta
}


def _load_log(date_str: str) -> list[dict]:
    path = LOGS_DIR / f"decisions_{date_str}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _format_record(rec: dict, use_color: bool = True) -> str:
    ts = rec.get("timestamp", "")[:19].replace("T", " ")
    component = rec.get("component", "?")
    action = rec.get("action", "?")
    outcome = rec.get("outcome", "?")
    reasoning = rec.get("reasoning", "")
    severity = rec.get("severity", "info")
    ticker = rec.get("ticker", "")

    color = COMPONENT_COLORS.get(component, "") if use_color else ""
    sev_color = SEVERITY_COLORS.get(severity, "") if use_color else ""
    reset = RESET if use_color else ""
    bold = BOLD if use_color else ""
    dim = DIM if use_color else ""

    ticker_str = f" [{ticker}]" if ticker else ""
    header = f"{dim}{ts}{reset} {color}{bold}[{component.upper()}]{reset}{ticker_str} {action} → {sev_color}{outcome}{reset}"
    body = f"  {dim}{reasoning}{reset}" if reasoning else ""

    return header + ("\n" + body if body else "")


def _print_summary(records: list[dict]) -> None:
    if not records:
        print("No records.")
        return

    from collections import Counter
    components = Counter(r.get("component") for r in records)
    outcomes = Counter(r.get("outcome") for r in records)
    severities = Counter(r.get("severity", "info") for r in records)
    tickers = Counter(r.get("ticker") for r in records if r.get("ticker"))

    print(f"\n{BOLD}Summary — {len(records)} entries{RESET}")
    print(f"\nBy component: " + ", ".join(f"{k}={v}" for k, v in components.most_common()))
    print(f"By outcome:   " + ", ".join(f"{k}={v}" for k, v in outcomes.most_common(8)))
    print(f"By severity:  " + ", ".join(f"{k}={v}" for k, v in severities.most_common()))
    if tickers:
        print(f"By ticker:    " + ", ".join(f"{k}={v}" for k, v in tickers.most_common(8)))

    action_required = [r for r in records if r.get("severity") == "action_required"]
    if action_required:
        print(f"\n{SEVERITY_COLORS['action_required']}{BOLD}Action Required ({len(action_required)}):{RESET}")
        for r in action_required[:5]:
            ticker_str = f"[{r.get('ticker')}] " if r.get("ticker") else ""
            print(f"  {ticker_str}{r.get('reasoning', '')[:100]}")


def main():
    parser = argparse.ArgumentParser(description="View agent decision logs")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                        help="Date to view (YYYY-MM-DD)")
    parser.add_argument("--component", help="Filter by component (monitor|scanner|content_engine|sms|scheduler)")
    parser.add_argument("--severity", help="Filter by severity (info|flag|action_required)")
    parser.add_argument("--ticker", help="Filter by ticker")
    parser.add_argument("--tail", type=int, help="Show last N entries")
    parser.add_argument("--summary", action="store_true", help="Show summary statistics only")
    parser.add_argument("--no-color", action="store_true", help="Disable color output")
    args = parser.parse_args()

    records = _load_log(args.date)

    if not records:
        print(f"No log entries found for {args.date}.")
        available = sorted(LOGS_DIR.glob("decisions_*.jsonl"), reverse=True)[:5] if LOGS_DIR.exists() else []
        if available:
            print("Available logs:")
            for p in available:
                print(f"  {p.stem.replace('decisions_', '')}")
        return

    if args.component:
        records = [r for r in records if r.get("component") == args.component]
    if args.severity:
        records = [r for r in records if r.get("severity") == args.severity]
    if args.ticker:
        records = [r for r in records if r.get("ticker", "").upper() == args.ticker.upper()]

    if args.summary:
        _print_summary(records)
        return

    if args.tail:
        records = records[-args.tail:]

    use_color = not args.no_color
    print(f"\n{BOLD}Decision Log — {args.date} ({len(records)} entries){RESET}\n")
    for rec in records:
        print(_format_record(rec, use_color=use_color))

    print()
    _print_summary(records)


if __name__ == "__main__":
    main()
