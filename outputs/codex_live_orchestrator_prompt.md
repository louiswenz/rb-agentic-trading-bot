# Codex Live Orchestrator Prompt

Run the Codex-orchestrated Robinhood adapter for the Agentic trading strategy.

Use these files as source of truth:

- `outputs/codex_robinhood_adapter_runbook.md`
- `outputs/strategy_config.json`
- `outputs/agentic_monitor.py`
- `outputs/agentic_task_queue.py`
- `outputs/update_agentic_state.py`
- `outputs/swing_strategy.py`
- `work/agentic_live_adapter_state.json`
- `work/agentic_tasks.json`
- `work/agentic_tasks.md`

Workflow:

1. Verify Robinhood tools are available. If unavailable, pause live trading and notify once.
2. Load `work/agentic_live_adapter_state.json`.
3. If `agentic_account_number` is empty, call `get_accounts` and resolve the account by nickname `Agentic`, account ending `6332`, `type=cash`, and `agentic_allowed=true`; persist the full account number only in `work/agentic_live_adapter_state.json`.
4. Fetch account, portfolio, equity positions, equity orders, option positions, option orders, and live equity quotes only for active positions, pending candidates, `SPY`, `QQQ`, `VIX`, and required risk/context symbols.
5. For candidate generation, always refresh full-universe local daily OHLCV history before scanning. Use `outputs/refresh_price_history.py`; if public history is unavailable, missing, or stale, fall back to Robinhood daily regular-hours historicals in small batches, save the raw JSON under `work/agentic_price_history_raw/`, convert it with `outputs/robinhood_historicals_to_prices.py`, and run `swing_strategy.py` against `work/agentic_price_history`. If any symbol remains missing or stale after both refresh paths, mark that symbol ineligible and report the data condition rather than skipping silently. SPY/QQQ/VIX are context and ranking inputs only when `strategy.market_regime_filter.enabled=false`; missing benchmark data should block only calculations that directly require that benchmark, not all new buys.
6. For candidate generation, use the token-efficient two-stage scan when `token_efficiency.deterministic_prescreen_before_news=true`: first run `swing_strategy.py --prescreen-news-symbols-only --json` without news to get `news_collection_symbols`, then collect latest stock-specific news only for those symbols unless a held position has a risk event. Convert news into the `--news-json` snapshot format from the runbook, including earnings timing fields when available. Missing news is neutral; severe adverse news and configured earnings blackout conditions block new buys.
7. Candidate scoring runs parallel setup engines: momentum breakout, momentum continuation, pullback-in-uptrend, sector-relative pullback, and quality range reversion. Sector-relative momentum is used as a setup/rank input when configured sector proxy history is fresh. The shared risk/news/live-price layer still decides whether any setup is tradable.
8. Options are long calls/puts only. Before any option review/place/cancel action, confirm the Agentic account is `agentic_allowed=true` and has `option_level_2` or `option_level_3`. If missing, block option trading only, continue equity monitoring, and notify once. Option candidate scans may enrich equity finalists with chains, instruments, and option quotes, but after-close scans remain candidate-only and must not perform broker order actions. During live monitor runs, fetch option positions, option orders, and option quotes; automatically review/place sell-to-close exits when the monitor returns `review_and_place_option_sell_to_close` for profit target, stop loss, time stop, or underlying stop.
9. Normalize snapshots and run `agentic_monitor.py`.
10. Persist the derived task queue to `work/agentic_tasks.json` and `work/agentic_tasks.md`. The queue must include queued buy reconciliation, protective-stop checks, profit-target checks, stop-ratchet checks, and option-exit checks.
11. Execute only returned actions allowed by hard caps through Robinhood review/place/cancel tools.
   - For equity protective stops, maintain exactly one open broker-side stop per symbol when `execution.consolidate_protective_stops_by_symbol=true`. On add-on fills or stop ratchets, cancel existing symbol stops first, then place one full-position GTC stop at the highest allowed stop price.
12. Save updated state. For local state stamps that are not already persisted by `agentic_monitor.py`, use `outputs/update_agentic_state.py` instead of inline `python3 -c` snippets.
13. Notify after every live monitor run, even when there are no actions or meaningful events. Keep the no-action update concise and include the live-monitor slot, account status, active positions, protective-stop status, pending candidates count, and next scheduled check. Candidate-only scans may stay quiet unless they stage candidates, hit a data/tool/risk condition, or otherwise need attention.

Schedule behavior:

- Schedule mode: regular market hours only.
- Candidate scans: separate cron at 6:00 AM PT, 10:00 AM PT, and 5:00 PM PT creates/updates pending candidates only and must not submit orders.
- The 10:00 AM PT intraday scan must use current account constraints, held symbols, live prices, and open-risk budget.
- The 5:00 PM PT after-close scan must refresh full-universe daily OHLC history, create/update pending candidates for next-session validation only, and perform no broker order actions.
- Regular market window: 6:30 AM to 1:00 PM PT, Monday through Friday, excluding market holidays.
- Current single-heartbeat envelope: weekdays at 6:00, 7:00, 8:00, 9:00, 10:00, 11:00, 12:00, 1:00, and 5:00 PT.
- Normal heartbeat inside market window: 1 hour.
- Elevated states: keep the 1-hour heartbeat when an order is active, first 30 minutes after entry, or price is within 1% of stop/target; live monitor runs still notify every time, with extra detail only on meaningful state changes.
- Morning validation: at or after 7:00 AM PT.
- Intraday rediscovery: at or after 10:00 AM PT, only if the twice-daily scanner has not already recorded the current 10:00 AM scan.
- Daily after-close candidate scan: 5:00 PM PT weekdays, candidate-only.
- Outside trading windows: no-op unless unresolved order/position risk exists or it is the 5:00 PM PT candidate-only scan.

Never invent quotes, fills, orders, account state, or tool results. Missing data means no trade.
