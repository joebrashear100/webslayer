"""
Event detection engine. Polls Alpaca for price moves, news, catalyst proximity,
rule violations, and pending entry windows. Returns a list of Event objects.
"""

import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from portfolio_config import PORTFOLIO, POSITION_TICKERS, ALL_TICKERS

logger = logging.getLogger(__name__)

PRICE_MOVE_THRESHOLD = 0.05   # 5%
CATALYST_PROXIMITY_DAYS = 7
NEWS_LOOKBACK_HOURS = 4
POSITION_CAP_PCT = 0.25       # 25% of spec book


@dataclass
class Event:
    ticker: str
    event_type: str    # price_move | news | catalyst_proximity | rule_alert | entry_window
    severity: str      # info | flag | action_required
    data: dict
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _get_trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
        paper=True,
    )


def _get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
    )


def detect_price_moves() -> list[Event]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    events = []
    client = _get_data_client()

    req = StockBarsRequest(
        symbol_or_symbols=POSITION_TICKERS,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=2),
        end=datetime.now(timezone.utc),
    )
    try:
        bars_map = client.get_stock_bars(req)
    except Exception as e:
        logger.error("price move fetch failed: %s", e)
        return events

    for ticker in POSITION_TICKERS:
        try:
            bars = bars_map[ticker]
            if len(bars) < 2:
                continue
            prev_close = bars[-2].close
            curr_close = bars[-1].close
            pct = (curr_close - prev_close) / prev_close
            if abs(pct) >= PRICE_MOVE_THRESHOLD:
                severity = "action_required" if abs(pct) >= 0.10 else "flag"
                events.append(Event(
                    ticker=ticker,
                    event_type="price_move",
                    severity=severity,
                    data={
                        "prev_close": prev_close,
                        "curr_close": curr_close,
                        "pct_change": round(pct * 100, 2),
                        "direction": "up" if pct > 0 else "down",
                        "thesis": PORTFOLIO["positions"][ticker].get("thesis", ""),
                    },
                ))
        except (KeyError, IndexError) as e:
            logger.warning("price move for %s: %s", ticker, e)

    return events


def detect_news_events() -> list[Event]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import NewsRequest

    events = []
    client = _get_data_client()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)

    for ticker in ALL_TICKERS:
        try:
            req = NewsRequest(symbols=[ticker], limit=10)
            articles = client.get_news(req)
            if not articles:
                continue
            recent = [a for a in articles if a.created_at.replace(tzinfo=timezone.utc) >= cutoff]
            if not recent:
                continue
            config = (
                PORTFOLIO["positions"].get(ticker)
                or PORTFOLIO["pending_entry"].get(ticker)
                or {}
            )
            events.append(Event(
                ticker=ticker,
                event_type="news",
                severity="flag",
                data={
                    "article_count": len(recent),
                    "headlines": [a.headline for a in recent[:3]],
                    "thesis": config.get("thesis", ""),
                },
            ))
        except Exception as e:
            logger.warning("news fetch for %s: %s", ticker, e)

    return events


def detect_catalyst_proximity() -> list[Event]:
    events = []
    today = date.today()

    for ticker, cfg in PORTFOLIO["positions"].items():
        for date_key in ("exit_date", "catalyst"):
            raw = cfg.get(date_key)
            if not raw:
                continue
            try:
                cat_date = date.fromisoformat(raw)
                delta = (cat_date - today).days
                if 0 <= delta <= CATALYST_PROXIMITY_DAYS:
                    severity = "action_required" if delta <= 2 else "flag"
                    events.append(Event(
                        ticker=ticker,
                        event_type="catalyst_proximity",
                        severity=severity,
                        data={
                            "catalyst_type": date_key,
                            "catalyst_date": raw,
                            "days_away": delta,
                            "exit_rule": cfg.get("exit_rule", ""),
                            "thesis": cfg.get("thesis", ""),
                        },
                    ))
            except ValueError:
                logger.warning("bad date for %s %s: %s", ticker, date_key, raw)

    # Pending entries
    for ticker, cfg in PORTFOLIO["pending_entry"].items():
        pdufa = cfg.get("pdufa")
        if pdufa:
            try:
                cat_date = date.fromisoformat(pdufa)
                delta = (cat_date - today).days
                if 0 <= delta <= CATALYST_PROXIMITY_DAYS:
                    events.append(Event(
                        ticker=ticker,
                        event_type="catalyst_proximity",
                        severity="flag",
                        data={
                            "catalyst_type": "pdufa",
                            "catalyst_date": pdufa,
                            "days_away": delta,
                            "thesis": cfg.get("thesis", ""),
                        },
                    ))
            except ValueError:
                pass

    return events


def detect_rule_violations() -> list[Event]:
    """Check position sizes against hard rules. Returns internal flag events only."""
    events = []
    try:
        trading_client = _get_trading_client()
        positions = trading_client.get_all_positions()
    except Exception as e:
        logger.error("could not fetch positions for rule check: %s", e)
        return events

    spec_book = PORTFOLIO["spec_book_size"]
    cap = POSITION_CAP_PCT * spec_book

    for pos in positions:
        try:
            market_value = float(pos.market_value)
            pct = market_value / spec_book
            if pct > POSITION_CAP_PCT:
                events.append(Event(
                    ticker=pos.symbol,
                    event_type="rule_alert",
                    severity="action_required",
                    data={
                        "rule": "No position > 25% of spec book",
                        "market_value": market_value,
                        "pct_of_book": round(pct * 100, 2),
                        "cap_usd": cap,
                    },
                ))
        except (ValueError, AttributeError) as e:
            logger.warning("rule check for %s: %s", pos.symbol, e)

    return events


def detect_entry_windows() -> list[Event]:
    events = []
    today = date.today()

    for ticker, cfg in PORTFOLIO["pending_entry"].items():
        try:
            start = date.fromisoformat(cfg["entry_window_start"])
            end = date.fromisoformat(cfg["entry_window_end"])
            if start <= today <= end:
                events.append(Event(
                    ticker=ticker,
                    event_type="entry_window",
                    severity="action_required",
                    data={
                        "target_shares": cfg["target_shares"],
                        "window_start": cfg["entry_window_start"],
                        "window_end": cfg["entry_window_end"],
                        "pdufa": cfg.get("pdufa", ""),
                        "thesis": cfg.get("thesis", ""),
                    },
                ))
        except (KeyError, ValueError) as e:
            logger.warning("entry window check for %s: %s", ticker, e)

    return events


def run_all_checks() -> list[Event]:
    all_events: list[Event] = []

    logger.info("running price move detection")
    all_events.extend(detect_price_moves())

    logger.info("running news detection")
    all_events.extend(detect_news_events())

    logger.info("running catalyst proximity detection")
    all_events.extend(detect_catalyst_proximity())

    logger.info("running rule violation detection")
    all_events.extend(detect_rule_violations())

    logger.info("running entry window detection")
    all_events.extend(detect_entry_windows())

    logger.info("monitor complete: %d events detected", len(all_events))
    return all_events


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    events = run_all_checks()
    for e in events:
        print(f"[{e.severity.upper()}] {e.ticker} — {e.event_type}: {e.data}")
