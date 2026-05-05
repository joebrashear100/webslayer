"""
Decision logger — writes every agent action, reasoning chain, and outcome
to structured JSONL for full auditability. Also maintains a human-readable
rolling log.

Usage (from any module):
    from decision_logger import log

    log.decision(
        component="monitor",
        action="price_move_check",
        reasoning="SNDK moved 6.2% — exceeds 5% threshold",
        data={"ticker": "SNDK", "pct": 6.2},
        outcome="event_created",
    )
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

_std_logger = logging.getLogger("decision_logger")


class DecisionLogger:
    def _jsonl_path(self) -> Path:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return LOGS_DIR / f"decisions_{date_str}.jsonl"

    def _txt_path(self) -> Path:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return LOGS_DIR / f"decisions_{date_str}.txt"

    def _write(self, record: dict) -> None:
        line = json.dumps(record, default=str)
        with open(self._jsonl_path(), "a") as f:
            f.write(line + "\n")

        ts = record["timestamp"]
        component = record.get("component", "")
        action = record.get("action", "")
        reasoning = record.get("reasoning", "")
        outcome = record.get("outcome", "")
        human_line = f"[{ts}] [{component.upper()}] {action} → {outcome}\n  Reasoning: {reasoning}\n"
        with open(self._txt_path(), "a") as f:
            f.write(human_line)

    def decision(
        self,
        component: str,
        action: str,
        reasoning: str,
        outcome: str,
        data: dict | None = None,
        severity: str = "info",
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": component,
            "action": action,
            "reasoning": reasoning,
            "outcome": outcome,
            "severity": severity,
            "data": data or {},
        }
        self._write(record)
        level = logging.WARNING if severity in ("flag", "action_required") else logging.INFO
        _std_logger.log(level, "[%s] %s → %s | %s", component, action, outcome, reasoning)

    def scan_result(
        self,
        ticker: str,
        scan_type: str,
        finding: str,
        reasoning: str,
        priority: str,
        data: dict | None = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": "scanner",
            "action": f"scan_{scan_type}",
            "ticker": ticker,
            "finding": finding,
            "reasoning": reasoning,
            "priority": priority,
            "data": data or {},
        }
        self._write(record)
        _std_logger.info("[SCAN/%s] %s → %s (priority: %s)", scan_type.upper(), ticker, finding, priority)

    def bedrock_reasoning(
        self,
        ticker: str,
        event_type: str,
        prompt_summary: str,
        full_reasoning: str,
        content_generated: str,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": "content_engine",
            "action": "bedrock_generation",
            "ticker": ticker,
            "event_type": event_type,
            "prompt_summary": prompt_summary,
            "full_reasoning": full_reasoning,
            "content_generated": content_generated,
        }
        self._write(record)
        _std_logger.info("[BEDROCK] %s/%s — reasoning logged (%d chars)", ticker, event_type, len(full_reasoning))

    def portfolio_evaluation(
        self,
        ticker: str,
        thesis_status: str,
        reasoning: str,
        recommended_action: str,
        data: dict | None = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": "evaluator",
            "action": "thesis_evaluation",
            "ticker": ticker,
            "thesis_status": thesis_status,
            "reasoning": reasoning,
            "recommended_action": recommended_action,
            "data": data or {},
        }
        self._write(record)
        _std_logger.info(
            "[EVAL] %s thesis=%s action=%s | %s",
            ticker, thesis_status, recommended_action, reasoning[:120],
        )

    def opportunity(
        self,
        ticker: str,
        thesis: str,
        reasoning: str,
        priority: str,
        data: dict | None = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": "scanner",
            "action": "opportunity_identified",
            "ticker": ticker,
            "thesis": thesis,
            "reasoning": reasoning,
            "priority": priority,
            "data": data or {},
        }
        self._write(record)
        _std_logger.info("[OPPORTUNITY/%s] %s | %s", priority.upper(), ticker, reasoning[:120])


# Singleton — import and use directly
log = DecisionLogger()
