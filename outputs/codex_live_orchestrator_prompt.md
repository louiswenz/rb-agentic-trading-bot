# Codex Live Orchestrator Prompt

Run the Codex-orchestrated Robinhood adapter for the Agentic trading strategy.

Use these files as source of truth:

- `outputs/codex_robinhood_adapter_runbook.md`
- `outputs/strategy_config.json`
- `outputs/agentic_monitor.py`
- `outputs/swing_strategy.py`
- `work/agentic_live_adapter_state.json`

Workflow:

1. Verify Robinhood tools are available. If unavailable, pause live trading and notify once.
2. Load `work/agentic_live_adapter_state.json`.
3. If `agentic_account_number` is empty, call `get_accounts` and resolve the account by nickname `Agentic`, account ending `6332`, `type=cash`, and `agentic_allowed=true`; persist the full account number only in `work/agentic_live_adapter_state.json`.
4. Fetch account, portfolio, equity positions, equity orders, and live equity quotes for active position, pending candidates, `SPY`, `VIX`, and required symbols.
5. For candidate generation, always refresh full-universe local daily OHLCV history before scanning. Use `outputs/refresh_price_history.py`; if public history is unavailable, missing, or stale, fall back to Robinhood daily regular-hours historicals in small batches, save the raw JSON under `work/agentic_price_history_raw/`, convert it with `outputs/robinhood_historicals_to_prices.py`, and run `swing_strategy.py` against `work/agentic_price_history`. If any symbol remains missing or stale after both refresh paths, mark that symbol ineligible and report the data condition rather than skipping silently; if benchmark or market-regime data is missing, block new buys.
6. For candidate generation, collect latest stock-specific news for each trade-universe symbol when available. Convert it into the `--news-json` snapshot format from the runbook, including earnings timing fields when available. Missing news is neutral; severe adverse news and configured earnings blackout conditions block new buys.
7. Normalize snapshots and run `agentic_monitor.py`.
8. Execute only returned actions allowed by hard caps through Robinhood review/place/cancel tools.
9. Save updated state.
10. Notify only on events.

Schedule behavior:

- Schedule mode: regular market hours only.
- Candidate scans: separate cron at 6:00 AM PT, 10:00 AM PT, and 5:00 PM PT creates/updates pending candidates only and must not submit orders.
- The 10:00 AM PT intraday scan must use current account constraints, held symbols, live prices, and open-risk budget.
- The 5:00 PM PT after-close scan must refresh full-universe daily OHLC history, create/update pending candidates for next-session validation only, and perform no broker order actions.
- Regular market window: 6:30 AM to 1:00 PM PT, Monday through Friday, excluding market holidays.
- Current single-heartbeat envelope: weekdays at 15-minute marks from 6:00 AM through 1:45 PM PT, with quiet no-op before 6:30 AM and after 1:00 PM unless unresolved protective-stop or open-order risk exists.
- Normal heartbeat inside market window: 15 minutes.
- Elevated states: keep the 15-minute heartbeat when an order is active, first 30 minutes after entry, or price is within 1% of stop/target; notify only on meaningful state changes.
- Morning validation: at or after 6:45 AM PT.
- Intraday rediscovery: at or after 10:00 AM PT, only if the twice-daily scanner has not already recorded the current 10:00 AM scan.
- Daily after-close candidate scan: 5:00 PM PT weekdays, candidate-only.
- Outside trading windows: no-op unless unresolved order/position risk exists.

Never invent quotes, fills, orders, account state, or tool results. Missing data means no trade.
