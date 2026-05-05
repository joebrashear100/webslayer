"""
Test suite for the Portfolio Intelligence Agent.
Uses mocked Alpaca/Bedrock calls — no live credentials required.
Run: python test_suite.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent))

# ── Helpers ────────────────────────────────────────────────────────────────────

PASS_COUNT = 0
FAIL_COUNT = 0
RESULTS_LOG = []


def record(name, passed, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if passed:
        PASS_COUNT += 1
        RESULTS_LOG.append(("PASS", name, detail))
        print(f"  [PASS] {name}")
    else:
        FAIL_COUNT += 1
        RESULTS_LOG.append(("FAIL", name, detail))
        print(f"  [FAIL] {name}: {detail}")


# ── Test 1: Portfolio Config ───────────────────────────────────────────────────

def test_portfolio_config():
    print("\n[1] Portfolio Config")
    try:
        from portfolio_config import PORTFOLIO, ALL_TICKERS, POSITION_TICKERS
        assert "MNKD" in PORTFOLIO["positions"]
        assert "TTD" in PORTFOLIO["positions"]
        assert "VRDN" in PORTFOLIO["pending_entry"]
        assert len(PORTFOLIO["hard_rules"]) == 5
        assert PORTFOLIO["spec_book_size"] == 8500
        assert "MNKD" in POSITION_TICKERS
        assert "VRDN" in ALL_TICKERS
        record("portfolio config loads correctly", True)
    except Exception as e:
        record("portfolio config loads correctly", False, str(e))


# ── Test 2: Event dataclass ────────────────────────────────────────────────────

def test_event_dataclass():
    print("\n[2] Event Dataclass")
    try:
        from monitor import Event
        e = Event(
            ticker="SNDK",
            event_type="price_move",
            severity="flag",
            data={"pct_change": 6.2, "direction": "up"},
        )
        assert e.ticker == "SNDK"
        assert e.event_type == "price_move"
        assert isinstance(e.timestamp, datetime)
        record("Event dataclass instantiates correctly", True)
    except Exception as e:
        record("Event dataclass instantiates correctly", False, str(e))


# ── Test 3: Price move detection (mocked) ─────────────────────────────────────

def test_price_move_detection():
    print("\n[3] Price Move Detection")

    fake_bar_old = MagicMock()
    fake_bar_old.close = 50.0
    fake_bar_new = MagicMock()
    fake_bar_new.close = 53.1  # 6.2% move

    fake_bars_map = {"SNDK": [fake_bar_old, fake_bar_new]}

    with patch("monitor._get_data_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.get_stock_bars.return_value = fake_bars_map
        mock_client_fn.return_value = mock_client

        from monitor import detect_price_moves
        events = detect_price_moves()

    sndk_events = [e for e in events if e.ticker == "SNDK" and e.event_type == "price_move"]
    try:
        assert len(sndk_events) == 1, f"expected 1 SNDK price_move event, got {len(sndk_events)}"
        assert sndk_events[0].data["pct_change"] == 6.2
        assert sndk_events[0].data["direction"] == "up"
        assert sndk_events[0].severity in ("flag", "action_required")
        record("6% price move on SNDK creates Event", True)
    except AssertionError as e:
        record("6% price move on SNDK creates Event", False, str(e))


# ── Test 4: No false positive below threshold ─────────────────────────────────

def test_price_move_no_false_positive():
    print("\n[4] Price Move Threshold Guard")

    fake_bar_old = MagicMock()
    fake_bar_old.close = 50.0
    fake_bar_new = MagicMock()
    fake_bar_new.close = 51.0  # 2% move — below threshold

    fake_bars_map = {t: [fake_bar_old, fake_bar_new] for t in ["MNKD", "TTD", "GEV", "SNDK", "VIST"]}

    with patch("monitor._get_data_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.get_stock_bars.return_value = fake_bars_map
        mock_client_fn.return_value = mock_client

        from monitor import detect_price_moves
        events = detect_price_moves()

    try:
        assert len(events) == 0, f"expected 0 events for 2% move, got {len(events)}"
        record("2% move does not trigger price_move event", True)
    except AssertionError as e:
        record("2% move does not trigger price_move event", False, str(e))


# ── Test 5: Content engine (mocked Bedrock) ───────────────────────────────────

def test_content_generation_bedrock():
    print("\n[5] Content Generation (Bedrock mock)")

    from monitor import Event
    test_event = Event(
        ticker="SNDK",
        event_type="price_move",
        severity="flag",
        data={
            "prev_close": 50.0,
            "curr_close": 53.1,
            "pct_change": 6.2,
            "direction": "up",
            "thesis": "NAND supercycle + HBF post-trim ride",
        },
    )

    mock_response_body = MagicMock()
    mock_response_body.read.return_value = json.dumps({
        "content": [{"text": "SNDK +6.2% today. NAND supercycle thesis intact. Still holding."}]
    }).encode()

    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model.return_value = {"body": mock_response_body}

    with patch("content_engine.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_bedrock
        from content_engine import generate_content
        result = generate_content(test_event)

    try:
        assert result["ticker"] == "SNDK"
        assert result["event_type"] == "price_move"
        assert "content" in result
        assert len(result["content"]) > 0
        assert result["bedrock_used"] is True
        record("content engine generates tweet via Bedrock mock", True)
    except AssertionError as e:
        record("content engine generates tweet via Bedrock mock", False, str(e))


# ── Test 6: Content engine fallback to stub ───────────────────────────────────

def test_content_generation_stub_fallback():
    print("\n[6] Content Generation (stub fallback)")

    from monitor import Event
    test_event = Event(
        ticker="SNDK",
        event_type="price_move",
        severity="flag",
        data={
            "prev_close": 50.0,
            "curr_close": 53.1,
            "pct_change": 6.2,
            "direction": "up",
            "thesis": "NAND supercycle + HBF post-trim ride",
        },
    )

    with patch("content_engine.boto3") as mock_boto3:
        mock_boto3.client.side_effect = Exception("Bedrock unavailable")
        from content_engine import generate_content
        result = generate_content(test_event)

    try:
        assert result["ticker"] == "SNDK"
        assert result["bedrock_used"] is False
        assert "[STUB]" in result["content"]
        record("stub fallback generates content when Bedrock unavailable", True)
    except AssertionError as e:
        record("stub fallback generates content when Bedrock unavailable", False, str(e))


# ── Test 7: Rule violation detection ─────────────────────────────────────────

def test_rule_violation_detection():
    print("\n[7] Rule Violation Detection")

    fake_position = MagicMock()
    fake_position.symbol = "MNKD"
    fake_position.market_value = "2300.00"  # 27% of $8500 spec book — over 25% cap

    with patch("monitor._get_trading_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = [fake_position]
        mock_client_fn.return_value = mock_client

        from monitor import detect_rule_violations
        events = detect_rule_violations()

    try:
        rule_events = [e for e in events if e.event_type == "rule_alert" and e.ticker == "MNKD"]
        assert len(rule_events) == 1, f"expected 1 rule_alert, got {len(rule_events)}"
        assert rule_events[0].severity == "action_required"
        assert rule_events[0].data["pct_of_book"] > 25
        record("26% position triggers rule_alert", True)
    except AssertionError as e:
        record("26% position triggers rule_alert", False, str(e))


# ── Test 8: Rule alert does not generate public content ───────────────────────

def test_rule_alert_internal_only():
    print("\n[8] Rule Alert — Internal Only")

    from monitor import Event
    rule_event = Event(
        ticker="MNKD",
        event_type="rule_alert",
        severity="action_required",
        data={
            "rule": "No position > 25% of spec book",
            "market_value": 2300.0,
            "pct_of_book": 27.1,
            "cap_usd": 2125.0,
        },
    )

    from content_engine import generate_content
    result = generate_content(rule_event)

    try:
        assert result["content_type"] == "internal"
        assert result["bedrock_used"] is False
        record("rule_alert produces internal content, not tweet", True)
    except AssertionError as e:
        record("rule_alert produces internal content, not tweet", False, str(e))


# ── Test 9: Draft queue write ─────────────────────────────────────────────────

def test_draft_queue_write():
    print("\n[9] Draft Queue Write")

    from monitor import Event
    test_event = Event(
        ticker="SNDK",
        event_type="price_move",
        severity="flag",
        data={"pct_change": 6.2, "direction": "up", "thesis": "NAND"},
    )
    content_result = {
        "event_type": "price_move",
        "ticker": "SNDK",
        "content": "SNDK +6.2% today. Thesis intact.",
        "content_type": "tweet",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bedrock_used": False,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("draft_queue.DRAFTS_DIR", Path(tmpdir)):
            from draft_queue import write_draft
            path = write_draft(test_event, content_result)

        try:
            assert path.exists()
            with open(path) as f:
                draft = json.load(f)
            assert draft["status"] == "pending"
            assert draft["event"]["ticker"] == "SNDK"
            assert draft["content"]["content_type"] == "tweet"
            assert "created_at" in draft
            record("draft written to queue with correct structure", True)
        except (AssertionError, json.JSONDecodeError) as e:
            record("draft written to queue with correct structure", False, str(e))


# ── Test 10: Scheduler initializes without error ──────────────────────────────

def test_scheduler_init():
    print("\n[10] Scheduler Initialization")
    try:
        from scheduler import build_scheduler
        scheduler = build_scheduler()
        jobs = scheduler.get_jobs()
        job_ids = {j.id for j in jobs}
        assert "intraday" in job_ids
        assert "eod" in job_ids
        assert "premarket" in job_ids
        assert len(jobs) == 3
        record("scheduler initializes with 3 jobs without error", True)
    except Exception as e:
        record("scheduler initializes with 3 jobs without error", False, str(e))


# ── Test 11: Catalyst proximity detection ─────────────────────────────────────

def test_catalyst_proximity():
    print("\n[11] Catalyst Proximity Detection")
    try:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with patch("monitor.PORTFOLIO", {
            "positions": {
                "TTD": {
                    "shares": 29.6854,
                    "thesis": "CTV play",
                    "catalyst": tomorrow,
                }
            },
            "pending_entry": {},
        }):
            with patch("monitor.POSITION_TICKERS", ["TTD"]):
                with patch("monitor.ALL_TICKERS", ["TTD"]):
                    from monitor import detect_catalyst_proximity
                    events = detect_catalyst_proximity()

        cat_events = [e for e in events if e.ticker == "TTD" and e.event_type == "catalyst_proximity"]
        assert len(cat_events) == 1
        assert cat_events[0].data["days_away"] == 1
        record("catalyst 1 day away creates catalyst_proximity event", True)
    except Exception as e:
        record("catalyst 1 day away creates catalyst_proximity event", False, str(e))


# ── Summary ────────────────────────────────────────────────────────────────────

def print_summary():
    print(f"\n{'='*50}")
    print(f"TEST RESULTS: {PASS_COUNT} passed / {FAIL_COUNT} failed")
    print(f"{'='*50}")
    if FAIL_COUNT > 0:
        print("\nFailed tests:")
        for status, name, detail in RESULTS_LOG:
            if status == "FAIL":
                print(f"  • {name}: {detail}")
    return FAIL_COUNT == 0


def main():
    print("Portfolio Intelligence Agent — Test Suite")
    print("=" * 50)

    test_portfolio_config()
    test_event_dataclass()
    test_price_move_detection()
    test_price_move_no_false_positive()
    test_content_generation_bedrock()
    test_content_generation_stub_fallback()
    test_rule_violation_detection()
    test_rule_alert_internal_only()
    test_draft_queue_write()
    test_scheduler_init()
    test_catalyst_proximity()

    ok = print_summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
