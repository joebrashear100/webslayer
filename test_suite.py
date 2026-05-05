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
        assert len(jobs) >= 3
        record("scheduler initializes with core jobs without error", True)
    except Exception as e:
        record("scheduler initializes with core jobs without error", False, str(e))


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


# ── Test 12: Decision logger writes JSONL ─────────────────────────────────────

def test_decision_logger_writes():
    print("\n[12] Decision Logger — JSONL output")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("decision_logger.LOGS_DIR", Path(tmpdir)):
            from decision_logger import DecisionLogger
            test_log = DecisionLogger()
            test_log.decision(
                component="test",
                action="test_action",
                reasoning="unit test reasoning",
                outcome="test_outcome",
                data={"key": "value"},
                severity="info",
            )

        jsonl_files = list(Path(tmpdir).glob("decisions_*.jsonl"))
        txt_files = list(Path(tmpdir).glob("decisions_*.txt"))
        try:
            assert len(jsonl_files) == 1, f"expected 1 JSONL file, got {len(jsonl_files)}"
            with open(jsonl_files[0]) as f:
                line = f.readline()
            record_data = json.loads(line)
            assert record_data["component"] == "test"
            assert record_data["action"] == "test_action"
            assert record_data["reasoning"] == "unit test reasoning"
            assert record_data["outcome"] == "test_outcome"
            assert len(txt_files) == 1, "expected human-readable .txt file"
            record("decision logger writes valid JSONL and TXT", True)
        except (AssertionError, json.JSONDecodeError) as e:
            record("decision logger writes valid JSONL and TXT", False, str(e))


# ── Test 13: Decision logger all methods work ─────────────────────────────────

def test_decision_logger_all_methods():
    print("\n[13] Decision Logger — all log methods")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("decision_logger.LOGS_DIR", Path(tmpdir)):
            from decision_logger import DecisionLogger
            test_log = DecisionLogger()
            try:
                test_log.scan_result("SNDK", "peer", "divergence 8%", "outperforming peers", "high")
                test_log.bedrock_reasoning("TTD", "price_move", "prompt", "full reasoning text", "tweet content")
                test_log.portfolio_evaluation("MNKD", "intact", "thesis holding", "hold")
                test_log.opportunity("VRDN", "binary FDA", "fits pattern", "high")
                lines = list(Path(tmpdir).glob("decisions_*.jsonl"))[0].read_text().strip().split("\n")
                assert len(lines) == 4, f"expected 4 log entries, got {len(lines)}"
                for line in lines:
                    json.loads(line)  # must be valid JSON
                record("all decision_logger methods produce valid JSONL", True)
            except Exception as e:
                record("all decision_logger methods produce valid JSONL", False, str(e))


# ── Test 14: Market scanner thesis evaluation (mocked Bedrock) ────────────────

def test_scanner_thesis_evaluation():
    print("\n[14] Market Scanner — thesis evaluation (mock)")

    mock_response_body = MagicMock()
    mock_response_body.read.return_value = json.dumps({
        "content": [{"text": json.dumps({
            "thesis_status": "intact",
            "top_signal": "No negative news, price stable",
            "recommended_action": "hold",
            "reasoning": "NAND thesis intact. Price action consistent with sector. No negative catalyst."
        })}]
    }).encode()

    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model.return_value = {"body": mock_response_body}

    fake_bar_old = MagicMock()
    fake_bar_old.close = 50.0
    fake_bar_new = MagicMock()
    fake_bar_new.close = 51.0

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("decision_logger.LOGS_DIR", Path(tmpdir)):
            with patch("market_scanner.boto3") as mock_boto3:
                mock_boto3.client.return_value = mock_bedrock
                with patch("market_scanner._get_recent_bars") as mock_bars:
                    mock_bars.return_value = {"SNDK": [fake_bar_old, fake_bar_new]}
                    with patch("market_scanner._get_recent_news") as mock_news:
                        mock_news.return_value = ["NAND prices stabilize in Q2"]
                        from market_scanner import evaluate_thesis
                        result = evaluate_thesis("SNDK")

    try:
        assert result is not None, "expected ScanResult, got None"
        assert result.ticker == "SNDK"
        assert result.scan_type == "thesis_check"
        assert result.data["thesis_status"] == "intact"
        assert result.data["recommended_action"] == "hold"
        record("scanner thesis evaluation returns correct ScanResult", True)
    except AssertionError as e:
        record("scanner thesis evaluation returns correct ScanResult", False, str(e))


# ── Test 15: Market scanner opportunity scan (mocked Bedrock) ─────────────────

def test_scanner_opportunity_scan():
    print("\n[15] Market Scanner — opportunity scan (mock)")

    mock_response_body = MagicMock()
    mock_response_body.read.return_value = json.dumps({
        "content": [{"text": json.dumps({
            "opportunities": [
                {
                    "ticker": "RXRX",
                    "thesis": "AI-driven drug discovery dislocation",
                    "catalyst": "Phase 2 data Q3 2026",
                    "fit_reasoning": "Fits binary biotech pattern similar to MNKD/VRDN",
                    "priority": "high",
                    "risk": "Phase 2 failure probability ~50%"
                }
            ],
            "macro_notes": "Biotech sector recovering from 2025 selloff."
        })}]
    }).encode()

    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model.return_value = {"body": mock_response_body}

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("decision_logger.LOGS_DIR", Path(tmpdir)):
            with patch("market_scanner.boto3") as mock_boto3:
                mock_boto3.client.return_value = mock_bedrock
                from market_scanner import scan_for_opportunities
                results = scan_for_opportunities()

    try:
        assert len(results) == 1, f"expected 1 opportunity, got {len(results)}"
        assert results[0].ticker == "RXRX"
        assert results[0].scan_type == "opportunity"
        assert results[0].priority == "high"
        record("scanner opportunity scan returns ScanResult", True)
    except AssertionError as e:
        record("scanner opportunity scan returns ScanResult", False, str(e))


# ── Test 16: Monitor logs decisions ───────────────────────────────────────────

def test_monitor_logs_decisions():
    print("\n[16] Monitor — decision logging wired in")

    import tempfile
    from pathlib import Path

    fake_bar_old = MagicMock()
    fake_bar_old.close = 50.0
    fake_bar_new = MagicMock()
    fake_bar_new.close = 53.1  # 6.2% — triggers event

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("decision_logger.LOGS_DIR", Path(tmpdir)):
            with patch("monitor._get_data_client") as mock_client_fn:
                mock_client = MagicMock()
                mock_client.get_stock_bars.return_value = {"SNDK": [fake_bar_old, fake_bar_new]}
                mock_client_fn.return_value = mock_client
                from monitor import detect_price_moves
                detect_price_moves()

            jsonl_files = list(Path(tmpdir).glob("decisions_*.jsonl"))
            try:
                assert len(jsonl_files) == 1
                lines = jsonl_files[0].read_text().strip().split("\n")
                records = [json.loads(l) for l in lines if l.strip()]
                components = {r["component"] for r in records}
                assert "monitor" in components, f"no monitor entries logged, got: {components}"
                sndk_entries = [r for r in records if r.get("data", {}).get("ticker") == "SNDK"]
                assert len(sndk_entries) > 0, "no SNDK-specific log entries found"
                record("monitor writes decision log entries for each check", True)
            except AssertionError as e:
                record("monitor writes decision log entries for each check", False, str(e))


# ── Test 17: Scheduler has 4 jobs (added scanner) ─────────────────────────────

def test_scheduler_has_four_jobs():
    print("\n[17] Scheduler — 4 jobs (monitor + scanner)")
    try:
        from scheduler import build_scheduler
        scheduler = build_scheduler()
        jobs = scheduler.get_jobs()
        job_ids = {j.id for j in jobs}
        assert "intraday" in job_ids
        assert "intraday_scan" in job_ids
        assert "eod" in job_ids
        assert "premarket" in job_ids
        assert len(jobs) == 4
        record("scheduler has 4 jobs including scanner", True)
    except Exception as e:
        record("scheduler has 4 jobs including scanner", False, str(e))


# ── Test 18: SMS kill switch suppresses sends ─────────────────────────────────

def test_sms_kill_switch():
    print("\n[18] SMS — kill switch suppresses sends")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / ".sms_rate_state.json"
        with patch.dict("os.environ", {"SMS_KILL_SWITCH": "true"}):
            with patch("sms_notifier._RATE_STATE_PATH", state_path):
                with patch("sms_notifier._send_raw") as mock_send:
                    from sms_notifier import send_alert
                    result = send_alert("test message", priority="critical", ticker="SNDK")
    try:
        assert result is False, "expected False when kill switch active"
        mock_send.assert_not_called()
        record("SMS kill switch suppresses all sends", True)
    except AssertionError as e:
        record("SMS kill switch suppresses all sends", False, str(e))


# ── Test 19: SMS rate limiter blocks after limit ───────────────────────────────

def test_sms_rate_limit():
    print("\n[19] SMS — rate limiter blocks excess sends")
    import tempfile
    from pathlib import Path

    now = datetime.now(timezone.utc)
    # Pre-fill state with 10 sends in last hour (at the limit)
    state = {
        "sent_timestamps": [(now - timedelta(minutes=i)).isoformat() for i in range(10)],
        "consecutive_failures": 0,
        "circuit_open_until": None,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / ".sms_rate_state.json"
        with open(state_path, "w") as f:
            json.dump(state, f)

        with patch.dict("os.environ", {"SMS_KILL_SWITCH": "false", "SMS_RATE_LIMIT_PER_HOUR": "10"}):
            with patch("sms_notifier._RATE_STATE_PATH", state_path):
                with patch("sms_notifier._send_raw") as mock_send:
                    from sms_notifier import send_alert
                    result = send_alert("over limit", priority="critical", ticker="TTD")

    try:
        assert result is False, "expected False when rate limited"
        mock_send.assert_not_called()
        record("SMS rate limiter blocks when at limit", True)
    except AssertionError as e:
        record("SMS rate limiter blocks when at limit", False, str(e))


# ── Test 20: SMS circuit breaker opens after failures ────────────────────────

def test_sms_circuit_breaker():
    print("\n[20] SMS — circuit breaker opens after 3 failures")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / ".sms_rate_state.json"

        with patch.dict("os.environ", {"SMS_KILL_SWITCH": "false"}):
            with patch("sms_notifier._RATE_STATE_PATH", state_path):
                with patch("sms_notifier._send_raw", side_effect=Exception("Twilio down")):
                    from sms_notifier import send_alert
                    for _ in range(3):
                        send_alert("fail", priority="critical", ticker="GEV")

                # Check circuit is now open
                with open(state_path) as f:
                    final_state = json.load(f)

    try:
        assert final_state["consecutive_failures"] >= 3
        assert final_state["circuit_open_until"] is not None, "circuit should be open"
        record("SMS circuit breaker opens after 3 consecutive failures", True)
    except AssertionError as e:
        record("SMS circuit breaker opens after 3 consecutive failures", False, str(e))


# ── Test 21: Security — env var validation ────────────────────────────────────

def test_security_env_validation():
    print("\n[21] Security — env var validation")
    with patch.dict("os.environ", {
        "AWS_ACCESS_KEY_ID": "test_key",
        "AWS_SECRET_ACCESS_KEY": "test_secret",
        "AWS_DEFAULT_REGION": "us-east-1",
        "BEDROCK_MODEL_ID": "anthropic.claude-sonnet-4-6",
        "ALPACA_API_KEY": "test_alpaca",
        "ALPACA_SECRET_KEY": "test_alpaca_secret",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
    }, clear=False):
        from security import validate_environment
        ok, issues = validate_environment()
    try:
        assert ok is True, f"expected all critical vars present, issues: {issues}"
        record("security validates environment with all critical vars set", True)
    except AssertionError as e:
        record("security validates environment with all critical vars set", False, str(e))


# ── Test 22: Security — missing critical var detected ─────────────────────────

def test_security_missing_critical():
    print("\n[22] Security — missing critical var detected")
    env = {k: v for k, v in __import__("os").environ.items()}
    env.pop("AWS_ACCESS_KEY_ID", None)
    env.pop("AWS_SECRET_ACCESS_KEY", None)

    with patch.dict("os.environ", {"AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": ""}, clear=False):
        from security import validate_environment
        ok, issues = validate_environment()
    try:
        assert ok is False, "expected failure with missing critical vars"
        record("security detects missing critical credentials", True)
    except AssertionError as e:
        record("security detects missing critical credentials", False, str(e))


# ── Test 23: Security — draft integrity seal and verify ───────────────────────

def test_draft_integrity():
    print("\n[23] Security — draft integrity seal + verify")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        draft_path = Path(tmpdir) / "test_draft.json"
        store_path = Path(tmpdir) / ".draft_integrity.json"
        draft_path.write_text(json.dumps({"status": "approved", "content": "test"}))

        with patch("security._INTEGRITY_STORE", store_path):
            from security import seal_draft, verify_draft

            digest = seal_draft(draft_path)
            valid, reason = verify_draft(draft_path)
            assert valid, f"fresh draft should verify: {reason}"

            # Tamper with the file
            draft_path.write_text(json.dumps({"status": "approved", "content": "TAMPERED"}))
            valid2, reason2 = verify_draft(draft_path)

    try:
        assert not valid2, "tampered draft should fail verification"
        assert "INTEGRITY FAILURE" in reason2, f"expected integrity failure message, got: {reason2}"
        record("draft integrity seal detects tampering", True)
    except AssertionError as e:
        record("draft integrity seal detects tampering", False, str(e))


# ── Test 24: Security — secrets scrubber ─────────────────────────────────────

def test_secrets_scrubber():
    print("\n[24] Security — secrets scrubber")
    from security import scrub

    test_cases = [
        ("My key is AKIAIOSFODNN7EXAMPLE", "[AWS_KEY_REDACTED]"),
        ("Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.sig", "[TOKEN_REDACTED]"),
    ]
    try:
        for input_text, expected_pattern in test_cases:
            result = scrub(input_text)
            assert expected_pattern in result or input_text != result, \
                f"scrubber should have modified: {input_text!r} → {result!r}"
        record("secrets scrubber redacts known patterns", True)
    except AssertionError as e:
        record("secrets scrubber redacts known patterns", False, str(e))


# ── Test 25: Status report generates without error ───────────────────────────

def test_status_report():
    print("\n[25] Status — report generates without error")
    try:
        from status import build_report
        report = build_report()
        assert "drafts" in report
        assert "last_monitor_cycle" in report
        assert "last_scanner_cycle" in report
        assert "sms" in report
        record("status report builds without error", True)
    except Exception as e:
        record("status report builds without error", False, str(e))


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
    test_decision_logger_writes()
    test_decision_logger_all_methods()
    test_scanner_thesis_evaluation()
    test_scanner_opportunity_scan()
    test_monitor_logs_decisions()
    test_scheduler_has_four_jobs()
    test_sms_kill_switch()
    test_sms_rate_limit()
    test_sms_circuit_breaker()
    test_security_env_validation()
    test_security_missing_critical()
    test_draft_integrity()
    test_secrets_scrubber()
    test_status_report()

    ok = print_summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
