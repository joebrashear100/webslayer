# Portfolio Intelligence Agent

Always-on portfolio monitor that detects thesis-relevant events, drafts Twitter content in the owner's voice, and queues drafts for human review before posting.

---

## Architecture Decision

**Path taken: Direct Bedrock InvokeModel + alpaca-py SDK**

The full Bedrock Agents framework (with Lambda-backed Action Groups and EventBridge) was evaluated but skipped in favor of direct `InvokeModel` calls + APScheduler. Rationale:

- Bedrock Agents require IAM role trust relationships, Lambda deployments, and S3 for agent knowledge bases — significant infrastructure overhead for a single-user system.
- Direct `InvokeModel` achieves identical output: structured prompts with portfolio context yield the same content quality.
- APScheduler handles scheduling in-process with the same cron semantics EventBridge would provide.
- Pivot is documented in `build_log.txt`.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env with real credentials
```

### 3. Run feasibility check

```bash
python feasibility_check.py
```

This verifies AWS/Bedrock, Alpaca, and Twitter connectivity before touching anything else. System halts if any CRITICAL service (Bedrock, Alpaca) fails.

### 4. Start the scheduler

```bash
python scheduler.py
```

On startup, runs an immediate monitor cycle, then schedules:
- Every 30 min during market hours (9:30am–4pm ET, weekdays)
- 4:30pm ET end-of-day summary
- 8:00am ET pre-market catalyst check

---

## File Layout

```
feasibility_check.py   — standalone service connectivity test
portfolio_config.py    — ground-truth portfolio positions and rules
monitor.py             — event detection (price moves, news, catalysts, rules)
content_engine.py      — Bedrock-powered tweet generation in owner's voice
queue.py               — draft file writer + Slack notifier
post_approved.py       — manual posting script (intentionally not automated)
scheduler.py           — APScheduler orchestration loop
test_suite.py          — full test suite with mocked dependencies
drafts/                — generated draft JSON files land here
build_log.txt          — pivots and decisions documented here
```

---

## Approval Workflow

1. `scheduler.py` detects events → generates drafts → writes to `drafts/`
2. Review draft JSON files manually (or get Slack notifications if `APPROVAL_MODE=slack`)
3. Edit `"status": "pending"` → `"status": "approved"` in any draft file
4. Run `python post_approved.py` to post approved drafts to Twitter

```bash
# Preview without posting
python post_approved.py --dry-run

# Post a specific file
python post_approved.py --file drafts/2026-05-05_10-00_SNDK_price_move.json

# Post all approved
python post_approved.py
```

---

## Switching Paper → Live Trading

1. In `.env`, change `ALPACA_BASE_URL` from `https://paper-api.alpaca.markets` to `https://api.alpaca.markets`
2. Replace `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` with live trading credentials (separate key pair from paper)
3. In `monitor.py` and anywhere `TradingClient` is instantiated, change `paper=True` to `paper=False`
4. Re-run `python feasibility_check.py` to confirm live connection

**Warning:** Live trading credentials have real financial consequences. The system itself does not place orders — it only monitors and drafts content. But ensure the live API key has read-only permissions if you want an additional safety layer.

---

## Known Limitations

- **No Lambda / EventBridge:** Scheduler runs in-process. If the process dies, monitoring stops. Use `systemd`, `supervisord`, or a Docker restart policy for production reliability.
- **Twitter rate limits:** The free Twitter API tier limits read/write volume. If you hit rate limits, drafts queue locally and posting will retry on next `post_approved.py` run.
- **Alpaca news latency:** News endpoint may lag real-time by minutes. For time-critical events, consider a supplemental news source.
- **Bedrock region:** Ensure your AWS region has Bedrock enabled and the target model is available. Model IDs are region-specific.
- **No order placement:** The agent never places or cancels orders. Pre-committed exit rules are surfaced as events and drafted content only — execution is always manual.

---

## Running Tests

```bash
python test_suite.py
```

All tests use mocked Alpaca and Bedrock clients — no live credentials required.
