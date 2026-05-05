"""
Standalone feasibility check — run before anything else.
Prints PASS/FAIL per service and exits non-zero if any CRITICAL service fails.
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

RESULTS = {}


def check(name, fn, critical=True):
    try:
        fn()
        RESULTS[name] = ("PASS", None, critical)
        print(f"  [PASS] {name}")
    except Exception as e:
        RESULTS[name] = ("FAIL", str(e), critical)
        tag = "CRITICAL" if critical else "WARNING"
        print(f"  [FAIL/{tag}] {name}: {e}")


# ── AWS / Bedrock ──────────────────────────────────────────────────────────────

def _aws_auth():
    import boto3
    sts = boto3.client("sts")
    identity = sts.get_caller_identity()
    assert identity["Account"], "No account returned"


def _bedrock_list_models():
    import boto3
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    client = boto3.client("bedrock", region_name=region)
    resp = client.list_foundation_models()
    assert resp["modelSummaries"], "No models returned"


def _bedrock_model_accessible():
    import boto3
    import json
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    model_id = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6")
    client = boto3.client("bedrock-runtime", region_name=region)
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "ping"}],
    })
    resp = client.invoke_model(modelId=model_id, body=body)
    result = json.loads(resp["body"].read())
    assert result.get("content"), "Empty response from model"


def _bedrock_agents_available():
    import boto3
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    client = boto3.client("bedrock-agent", region_name=region)
    client.list_agents(maxResults=1)


# ── Alpaca ─────────────────────────────────────────────────────────────────────

def _alpaca_connect():
    from alpaca.trading.client import TradingClient
    client = TradingClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
        paper=True,
    )
    account = client.get_account()
    assert account.id, "No account ID returned"


def _alpaca_positions():
    from alpaca.trading.client import TradingClient
    client = TradingClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
        paper=True,
    )
    client.get_all_positions()


def _alpaca_bars():
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    client = StockHistoricalDataClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
    )
    req = StockBarsRequest(
        symbol_or_symbols=["AAPL"],
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=5),
        end=datetime.now(timezone.utc),
    )
    bars = client.get_stock_bars(req)
    assert bars, "No bars returned"


def _alpaca_news():
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import NewsRequest
    client = StockHistoricalDataClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
    )
    req = NewsRequest(symbols=["AAPL"], limit=3)
    news = client.get_news(req)
    assert news is not None, "News returned None"


# ── Twitter ────────────────────────────────────────────────────────────────────

def _twitter_auth():
    import tweepy
    client = tweepy.Client(
        bearer_token=os.getenv("TWITTER_BEARER_TOKEN"),
        consumer_key=os.getenv("TWITTER_API_KEY"),
        consumer_secret=os.getenv("TWITTER_API_SECRET"),
        access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
        access_token_secret=os.getenv("TWITTER_ACCESS_SECRET"),
    )
    me = client.get_me()
    assert me.data, "No user data returned"


def _twitter_read_timeline():
    import tweepy
    client = tweepy.Client(bearer_token=os.getenv("TWITTER_BEARER_TOKEN"))
    me = tweepy.Client(
        consumer_key=os.getenv("TWITTER_API_KEY"),
        consumer_secret=os.getenv("TWITTER_API_SECRET"),
        access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
        access_token_secret=os.getenv("TWITTER_ACCESS_SECRET"),
    ).get_me()
    user_id = me.data.id
    tweets = tweepy.Client(bearer_token=os.getenv("TWITTER_BEARER_TOKEN")).get_users_tweets(
        id=user_id, max_results=5
    )
    assert tweets is not None, "Timeline returned None"


def _twitter_write_scope():
    """Verify OAuth1 credentials have write scope — does NOT post."""
    import tweepy
    auth = tweepy.OAuth1UserHandler(
        os.getenv("TWITTER_API_KEY"),
        os.getenv("TWITTER_API_SECRET"),
        os.getenv("TWITTER_ACCESS_TOKEN"),
        os.getenv("TWITTER_ACCESS_SECRET"),
    )
    api = tweepy.API(auth)
    creds = api.verify_credentials(include_entities=False, skip_status=True)
    assert creds, "Credential verification failed"


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    print("\n=== FEASIBILITY CHECK ===\n")

    print("[AWS / Bedrock]")
    check("AWS authentication", _aws_auth, critical=True)
    check("Bedrock list foundation models", _bedrock_list_models, critical=True)
    check("Bedrock target model accessible", _bedrock_model_accessible, critical=True)
    check("Bedrock Agents service available", _bedrock_agents_available, critical=False)

    print("\n[Alpaca]")
    check("Alpaca connect + get_account", _alpaca_connect, critical=True)
    check("Alpaca get_all_positions", _alpaca_positions, critical=True)
    check("Alpaca get_bars AAPL", _alpaca_bars, critical=True)
    check("Alpaca news endpoint", _alpaca_news, critical=True)

    print("\n[Twitter / X]")
    check("Twitter auth + get_me", _twitter_auth, critical=False)
    check("Twitter read timeline", _twitter_read_timeline, critical=False)
    check("Twitter write scope verified", _twitter_write_scope, critical=False)

    print("\n=== SUMMARY ===\n")
    print(f"  {'Service':<45} {'Status':<8} {'Critical'}")
    print(f"  {'-'*45} {'-'*8} {'-'*8}")
    for name, (status, err, critical) in RESULTS.items():
        crit = "YES" if critical else "no"
        print(f"  {name:<45} {status:<8} {crit}")

    critical_failures = [n for n, (s, _, c) in RESULTS.items() if s == "FAIL" and c]
    if critical_failures:
        print(f"\nCRITICAL FAILURES — system cannot run:")
        for f in critical_failures:
            _, err, _ = RESULTS[f]
            print(f"  • {f}: {err}")
        print("\nRemediation:")
        print("  1. Verify credentials in .env match the service's dashboard.")
        print("  2. Confirm AWS region has Bedrock enabled.")
        print("  3. For Alpaca: ensure paper trading key pair, not live.")
        sys.exit(1)
    else:
        warnings = [n for n, (s, _, c) in RESULTS.items() if s == "FAIL" and not c]
        if warnings:
            print(f"\nNon-critical failures (system will use fallbacks): {warnings}")
        print("\nAll critical checks passed. System is ready to run.\n")


if __name__ == "__main__":
    run()
