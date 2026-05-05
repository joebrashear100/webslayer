"""
Market scanner — continuously evaluates portfolio thesis validity, scans for
peer/sector signals, and identifies new opportunities aligned with the
portfolio's thesis patterns.

Runs on a schedule alongside the monitor. All findings are logged via
decision_logger and generate ScanResult objects that feed into content_engine.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import boto3
from dotenv import load_dotenv

load_dotenv()

from portfolio_config import PORTFOLIO, POSITION_TICKERS, ALL_TICKERS
from decision_logger import log

logger = logging.getLogger(__name__)

THESIS_PEERS = {
    "MNKD": ["INVA", "ORMP", "NKTR"],
    "TTD": ["PUBM", "MGNI", "DV"],
    "GEV": ["NEE", "VST", "CEG"],
    "SNDK": ["MU", "WDC", "KIOXIA"],
    "VIST": ["YPF", "PAM", "TGS"],
    "VRDN": ["ACAD", "SIGA", "PRLD"],
}

SECTOR_ETFS = {
    "biotech": "XBI",
    "tech": "QQQ",
    "energy": "XLE",
    "semiconductors": "SOXX",
}


@dataclass
class ScanResult:
    ticker: str
    scan_type: str       # thesis_check | peer_signal | opportunity | macro_alert | thesis_drift
    priority: str        # low | medium | high | critical
    finding: str
    reasoning: str
    data: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
    )


def _invoke_bedrock(prompt: str, max_tokens: int = 1024) -> str:
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    model_id = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6")
    client = boto3.client("bedrock-runtime", region_name=region)
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    })
    resp = client.invoke_model(modelId=model_id, body=body)
    return json.loads(resp["body"].read())["content"][0]["text"].strip()


def _get_recent_bars(tickers: list[str], days: int = 5) -> dict:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    if not tickers:
        return {}
    try:
        client = _get_data_client()
        req = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=days),
            end=datetime.now(timezone.utc),
        )
        return dict(client.get_stock_bars(req))
    except Exception as e:
        logger.warning("bar fetch failed for %s: %s", tickers, e)
        return {}


def _get_recent_news(ticker: str, limit: int = 5) -> list[str]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import NewsRequest

    try:
        client = _get_data_client()
        req = NewsRequest(symbols=[ticker], limit=limit)
        articles = client.get_news(req)
        return [a.headline for a in (articles or [])]
    except Exception as e:
        logger.warning("news fetch for %s: %s", ticker, e)
        return []


def evaluate_thesis(ticker: str) -> ScanResult | None:
    """Ask Bedrock to evaluate whether the thesis for a position still holds."""
    cfg = PORTFOLIO["positions"].get(ticker) or PORTFOLIO["pending_entry"].get(ticker)
    if not cfg:
        return None

    thesis = cfg.get("thesis", "")
    headlines = _get_recent_news(ticker, limit=8)
    bars = _get_recent_bars([ticker], days=10)
    bar_summary = ""
    if ticker in bars and bars[ticker]:
        recent = bars[ticker][-5:]
        prices = [f"{b.close:.2f}" for b in recent]
        bar_summary = f"Recent closes: {', '.join(prices)}"

    prompt = f"""You are evaluating an investment thesis for a speculative equity position.

Ticker: {ticker}
Thesis: {thesis}
{bar_summary}
Recent headlines: {json.dumps(headlines, indent=2)}

Evaluate:
1. Is the thesis still intact? (intact | weakening | broken | accelerating)
2. What is the highest-priority signal from the recent data?
3. What action should the investor consider? (hold | add | reduce | exit | watch)
4. Summarize your reasoning in 2-3 sentences.

Respond in this exact JSON format:
{{
  "thesis_status": "intact|weakening|broken|accelerating",
  "top_signal": "one sentence",
  "recommended_action": "hold|add|reduce|exit|watch",
  "reasoning": "2-3 sentence reasoning"
}}"""

    try:
        raw = _invoke_bedrock(prompt)
        parsed = json.loads(raw)
        thesis_status = parsed.get("thesis_status", "unknown")
        reasoning = parsed.get("reasoning", "")
        action = parsed.get("recommended_action", "watch")
        top_signal = parsed.get("top_signal", "")

        priority = {
            "broken": "critical",
            "weakening": "high",
            "accelerating": "medium",
            "intact": "low",
        }.get(thesis_status, "low")

        log.portfolio_evaluation(
            ticker=ticker,
            thesis_status=thesis_status,
            reasoning=reasoning,
            recommended_action=action,
            data={"top_signal": top_signal, "headlines_count": len(headlines)},
        )

        return ScanResult(
            ticker=ticker,
            scan_type="thesis_check",
            priority=priority,
            finding=f"Thesis {thesis_status} — {action}",
            reasoning=reasoning,
            data={
                "thesis_status": thesis_status,
                "recommended_action": action,
                "top_signal": top_signal,
            },
        )
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("thesis eval parse error for %s: %s", ticker, e)
        log.decision(
            component="scanner",
            action="thesis_evaluation",
            reasoning=f"Bedrock response could not be parsed: {e}",
            outcome="skipped",
            data={"ticker": ticker},
            severity="flag",
        )
        return None
    except Exception as e:
        logger.error("thesis eval failed for %s: %s", ticker, e)
        return None


def scan_peers(ticker: str) -> list[ScanResult]:
    """Compare a position's price action against its peer group."""
    peers = THESIS_PEERS.get(ticker, [])
    if not peers:
        return []

    all_tickers = [ticker] + peers
    bars = _get_recent_bars(all_tickers, days=5)
    results = []

    position_move = None
    if ticker in bars and len(bars[ticker]) >= 2:
        b = bars[ticker]
        position_move = (b[-1].close - b[0].close) / b[0].close * 100

    peer_moves = {}
    for peer in peers:
        if peer in bars and len(bars[peer]) >= 2:
            b = bars[peer]
            peer_moves[peer] = (b[-1].close - b[0].close) / b[0].close * 100

    if position_move is None or not peer_moves:
        log.decision(
            component="scanner",
            action="peer_scan",
            reasoning=f"Insufficient bar data for {ticker} peer comparison",
            outcome="skipped",
            data={"ticker": ticker, "peers": peers},
        )
        return results

    avg_peer = sum(peer_moves.values()) / len(peer_moves)
    divergence = position_move - avg_peer

    if abs(divergence) >= 5:
        direction = "outperforming" if divergence > 0 else "underperforming"
        priority = "high" if abs(divergence) >= 10 else "medium"
        finding = f"{ticker} {direction} peers by {abs(divergence):.1f}% over 5 days"
        reasoning = (
            f"{ticker} moved {position_move:+.1f}% while peers averaged {avg_peer:+.1f}%. "
            f"Divergence of {divergence:+.1f}% may indicate idiosyncratic signal worth investigating."
        )
        log.scan_result(
            ticker=ticker,
            scan_type="peer_comparison",
            finding=finding,
            reasoning=reasoning,
            priority=priority,
            data={"position_move_pct": position_move, "avg_peer_move_pct": avg_peer, "peer_moves": peer_moves},
        )
        results.append(ScanResult(
            ticker=ticker,
            scan_type="peer_signal",
            priority=priority,
            finding=finding,
            reasoning=reasoning,
            data={"position_move": position_move, "avg_peer": avg_peer, "peer_moves": peer_moves},
        ))
    else:
        log.scan_result(
            ticker=ticker,
            scan_type="peer_comparison",
            finding=f"In-line with peers (divergence {divergence:+.1f}%)",
            reasoning="No significant peer divergence detected.",
            priority="low",
        )

    return results


def scan_for_opportunities() -> list[ScanResult]:
    """
    Ask Bedrock to identify new speculative opportunities aligned with the
    portfolio's established thesis patterns (binary catalysts, dislocations,
    macro plays). Returns ScanResult objects for review.
    """
    existing_theses = {
        t: cfg.get("thesis", "")
        for t, cfg in {**PORTFOLIO["positions"], **PORTFOLIO["pending_entry"]}.items()
    }

    prompt = f"""You are a speculative equity analyst reviewing an investor's portfolio thesis patterns.

Current portfolio theses:
{json.dumps(existing_theses, indent=2)}

Portfolio hard rules:
{json.dumps(PORTFOLIO['hard_rules'], indent=2)}

Spec book size: ${PORTFOLIO['spec_book_size']:,}
Max position: 25% (~$2,125)

Task: Identify 2-3 high-conviction speculative opportunities that:
1. Fit the investor's established thesis patterns (binary FDA catalysts, sector dislocations, macro plays, tech recovery)
2. Are NOT already in the portfolio
3. Respect the hard rules (no position >25%, max 2 unresolved binaries)
4. Have a specific near-term catalyst or edge

For each opportunity, state: ticker, thesis, catalyst, why it fits this portfolio's pattern, and priority (low/medium/high).

Respond in this exact JSON format:
{{
  "opportunities": [
    {{
      "ticker": "XXXX",
      "thesis": "one sentence",
      "catalyst": "specific event and date if known",
      "fit_reasoning": "why this fits the portfolio pattern",
      "priority": "low|medium|high",
      "risk": "primary risk in one sentence"
    }}
  ],
  "macro_notes": "any relevant macro context in 1-2 sentences"
}}"""

    results = []
    try:
        raw = _invoke_bedrock(prompt, max_tokens=1500)
        parsed = json.loads(raw)
        opportunities = parsed.get("opportunities", [])
        macro_notes = parsed.get("macro_notes", "")

        if macro_notes:
            log.decision(
                component="scanner",
                action="macro_context",
                reasoning=macro_notes,
                outcome="logged",
            )

        for opp in opportunities:
            ticker = opp.get("ticker", "UNKNOWN")
            thesis = opp.get("thesis", "")
            fit_reasoning = opp.get("fit_reasoning", "")
            priority = opp.get("priority", "low")
            catalyst = opp.get("catalyst", "")
            risk = opp.get("risk", "")

            log.opportunity(
                ticker=ticker,
                thesis=thesis,
                reasoning=fit_reasoning,
                priority=priority,
                data={"catalyst": catalyst, "risk": risk, "macro_notes": macro_notes},
            )

            results.append(ScanResult(
                ticker=ticker,
                scan_type="opportunity",
                priority=priority,
                finding=f"New opportunity: {thesis}",
                reasoning=f"{fit_reasoning} | Catalyst: {catalyst} | Risk: {risk}",
                data={"catalyst": catalyst, "risk": risk, "macro_notes": macro_notes},
            ))

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("opportunity scan parse error: %s", e)
        log.decision(
            component="scanner",
            action="opportunity_scan",
            reasoning=f"Bedrock response parse failed: {e}",
            outcome="skipped",
            severity="flag",
        )
    except Exception as e:
        logger.error("opportunity scan failed: %s", e)

    return results


def detect_thesis_drift(ticker: str) -> ScanResult | None:
    """
    Detect if a position's price action or news has drifted from its
    thesis without a formal catalyst event.
    """
    cfg = PORTFOLIO["positions"].get(ticker)
    if not cfg:
        return None

    thesis = cfg.get("thesis", "")
    headlines = _get_recent_news(ticker, limit=10)
    bars = _get_recent_bars([ticker], days=20)

    if ticker not in bars or len(bars[ticker]) < 10:
        return None

    recent_bars = bars[ticker]
    prices = [b.close for b in recent_bars]
    high = max(prices)
    low = min(prices)
    current = prices[-1]
    drawdown_from_high = (current - high) / high * 100
    range_pct = (high - low) / low * 100

    prompt = f"""Analyze whether this position has drifted from its thesis.

Ticker: {ticker}
Original thesis: {thesis}
20-day price range: ${low:.2f} - ${high:.2f} ({range_pct:.1f}% range)
Current price: ${current:.2f} ({drawdown_from_high:.1f}% from 20-day high)
Recent headlines: {json.dumps(headlines[:5], indent=2)}

Has the price action or news indicated thesis drift (the original reason to own it no longer applies)?
Answer with: drift_detected (true/false), severity (none/minor/major), and reasoning (1-2 sentences).

JSON format:
{{
  "drift_detected": true,
  "severity": "none|minor|major",
  "reasoning": "explanation"
}}"""

    try:
        raw = _invoke_bedrock(prompt, max_tokens=256)
        parsed = json.loads(raw)
        drift = parsed.get("drift_detected", False)
        severity = parsed.get("severity", "none")
        reasoning = parsed.get("reasoning", "")

        if not drift or severity == "none":
            log.decision(
                component="scanner",
                action="thesis_drift_check",
                reasoning=f"No drift detected for {ticker}: {reasoning}",
                outcome="no_action",
                data={"ticker": ticker, "severity": severity},
            )
            return None

        priority = "critical" if severity == "major" else "high"
        log.decision(
            component="scanner",
            action="thesis_drift_check",
            reasoning=reasoning,
            outcome=f"drift_{severity}",
            data={"ticker": ticker, "severity": severity, "drawdown_from_high": drawdown_from_high},
            severity="action_required" if severity == "major" else "flag",
        )

        return ScanResult(
            ticker=ticker,
            scan_type="thesis_drift",
            priority=priority,
            finding=f"Thesis drift detected ({severity})",
            reasoning=reasoning,
            data={"severity": severity, "drawdown_from_high": drawdown_from_high, "range_pct": range_pct},
        )

    except Exception as e:
        logger.warning("drift check failed for %s: %s", ticker, e)
        return None


def run_full_scan() -> list[ScanResult]:
    """Run all scanner modules and return combined results, prioritized."""
    all_results: list[ScanResult] = []

    log.decision(
        component="scanner",
        action="full_scan_start",
        reasoning="Scheduled full market scan initiated",
        outcome="running",
    )

    # Thesis evaluation for each position
    for ticker in POSITION_TICKERS:
        logger.info("evaluating thesis: %s", ticker)
        result = evaluate_thesis(ticker)
        if result:
            all_results.append(result)

    # Peer comparison
    for ticker in POSITION_TICKERS:
        logger.info("scanning peers: %s", ticker)
        all_results.extend(scan_peers(ticker))

    # Thesis drift detection
    for ticker in POSITION_TICKERS:
        logger.info("checking thesis drift: %s", ticker)
        result = detect_thesis_drift(ticker)
        if result:
            all_results.append(result)

    # New opportunity scan
    logger.info("scanning for new opportunities")
    all_results.extend(scan_for_opportunities())

    # Sort by priority
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_results.sort(key=lambda r: priority_order.get(r.priority, 99))

    log.decision(
        component="scanner",
        action="full_scan_complete",
        reasoning=f"Scan complete: {len(all_results)} findings across thesis checks, peer comparisons, drift detection, opportunities",
        outcome=f"{len(all_results)}_findings",
        data={
            "critical": sum(1 for r in all_results if r.priority == "critical"),
            "high": sum(1 for r in all_results if r.priority == "high"),
            "medium": sum(1 for r in all_results if r.priority == "medium"),
            "low": sum(1 for r in all_results if r.priority == "low"),
        },
    )

    return all_results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = run_full_scan()
    for r in results:
        print(f"[{r.priority.upper()}] {r.ticker} / {r.scan_type}: {r.finding}")
        print(f"  {r.reasoning}\n")
