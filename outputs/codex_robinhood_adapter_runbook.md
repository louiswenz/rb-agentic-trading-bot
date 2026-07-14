# Codex-Orchestrated Robinhood Adapter Runbook

This runbook defines the live adapter layer for the Agentic trading strategy. The local Python scripts produce state, events, and action requests. Codex automations call Robinhood tools, feed snapshots into `agentic_monitor.py`, execute returned actions, save state, and notify the user.

The monitor also derives an operational task queue from broker snapshots and local state. Persist it after every meaningful monitor run:

- Machine-readable queue: `work/agentic_tasks.json`
- Human-readable queue: `work/agentic_tasks.md`

For local state stamps that are not already saved by `agentic_monitor.py`, use `outputs/update_agentic_state.py` instead of inline `python3 -c` snippets. This keeps heartbeat permissions scoped to a repo-local helper while still writing only `work/agentic_live_adapter_state.json`.

## Standing Authorization

Live automation is authorized only within these hard caps:

- Agentic account only.
- Max 2 automatic new buys per trading day.
- Under $5,000 account value: max 2 open positions.
- Max 50% account exposure per position.
- Max 4% planned risk per trade.
- Max 6% planned risk across all open positions.
- Minimum 1.5R reward/risk for new buys.
- Sector/group concentration is disabled; same-symbol add-ons are allowed when all risk caps pass.
- Earnings blackout blocks new buys when supplied news data shows earnings within 5 trading days.
- Pause new buys at 5% daily drawdown from start-of-day equity.
- Limit buy orders only.
- Stop-market GTC protective sell after every buy fill.
- R-based synthetic profit target only unless native linked OCO/bracket support is available; default partial target is 1R and default full target is 1.5R.
- Long call/put options are allowed only after the Agentic account reports `option_level_2` or `option_level_3`; no crypto, futures, margin, shorts, market buys, leveraged ETFs, unsettled-cash reuse, multi-leg options, covered calls, cash-secured puts, naked shorts, or stock-option combo orders.

## Tool Mapping

Codex must use Robinhood tools directly. The local Python scripts cannot call these tools.

### Account Resolution

Load `work/agentic_live_adapter_state.json`. If `agentic_account_number` is empty, call `get_accounts` and select the account matching:

- nickname `Agentic`, or
- masked account ending `6332`, and
- `agentic_allowed=true`, and
- `type=cash`.

Persist the full account number only in `work/agentic_live_adapter_state.json`; do not print it in user-facing output.

### Snapshot Phase

Use read-only Robinhood tools:

- `get_accounts`: identify the Agentic account and confirm `agentic_allowed=true`.
- `get_portfolio`: equity, buying power, cash, asset values.
- `get_equity_positions`: open stock/ETF positions.
- `get_equity_orders`: open/recent orders.
- `get_equity_quotes`: live quotes for active positions, pending candidates, `SPY`, `QQQ`, `VIX`, and required risk/context symbols.
- `get_equity_historicals`: daily regular-hours OHLCV bars for the benchmark and trade-universe symbols.
- `get_option_positions`: open option positions.
- `get_option_orders`: open/recent option orders.
- `get_option_chains`, `get_option_instruments`, `get_option_quotes`: option contract discovery and quote validation for final equity-signal candidates only.
- Latest stock news snapshot for deterministic prescreen survivors and active holdings with risk events when available from connected tools or approved web/news sources.

Normalize snapshots into:

```json
{
  "account": {
    "equity": 2000.0,
    "buying_power": 2000.0,
    "positions": []
  },
  "orders": [],
  "quotes": {
    "QQQ": {"price": 138.8},
    "SPY": {"price": 600.0},
    "VIX": {"price": 18.0}
  }
}
```

For token-efficient daily candidate generation, first run the deterministic prescreen without news:

```bash
python3 outputs/swing_strategy.py \
  --config outputs/strategy_config.json \
  --prices-dir work/agentic_price_history \
  --account-value ACCOUNT_VALUE \
  --settled-cash SETTLED_CASH \
  --monthly-start-equity MONTHLY_START_EQUITY \
  --positions-count POSITIONS_COUNT \
  --held-symbols HELD_SYMBOLS_JSON \
  --open-risk-dollars OPEN_RISK_DOLLARS \
  --prescreen-news-symbols-only \
  --json
```

Collect or summarize stock-specific news only for the returned `news_collection_symbols`, plus active holdings with risk events. Then pass that smaller news snapshot into `swing_strategy.py` with `--news-json`. The snapshot is keyed by symbol and may include:

```json
{
  "AMD": {
    "sentiment_score": 1.5,
    "summary": "analyst upgrade and product launch",
    "headlines": ["..."],
    "material_events": ["analyst upgrade"],
    "blocking_event": false
  }
}
```

Use a -3 to +3 sentiment scale. Missing news is neutral by default. Severe adverse news or a configured blocking event must prevent a new buy candidate even when the technical setup passes.
When available, include `days_until_earnings`, `next_earnings_date`, or `earnings_date`; the scanner blocks new buys inside the configured earnings blackout window.

### Candidate Setup Families

`swing_strategy.py` evaluates setup engines in parallel and emits the highest-ranked valid setup for each symbol:

- `momentum_breakout`: price is breaking above the prior range with required trend, risk, and volume context.
- `momentum_continuation`: price remains in a valid trend and relative-strength continuation pattern without a fresh range breakout.
- `pullback_in_uptrend`: price has pulled back into a rising trend, remains near the configured EMA, has acceptable RSI, and meets lower pullback volume confirmation.
- `sector_relative_pullback`: the sector is holding up versus SPY while the stock has temporarily lagged the sector and is reclaiming support.
- `quality_range_reversion`: a liquid large-cap or ETF-style symbol has pulled back inside a broad uptrend and shows a bullish reversal near range support.

After setup evaluation, all candidates still pass the shared news, reward/risk, buying-power, cash-reserve, position-size, liquidity, max-loss, and live-price validation layers. Broker positions and open orders must be supplied to the scanner when available so held-symbol add-ons are capped by aggregate post-trade symbol exposure, not only by the incremental order size. When `strategy.sector_relative_momentum.enabled=true`, the scanner loads the configured sector proxy CSVs from `work/agentic_price_history`, applies sector-relative inputs to relevant setup engines, and adds the configured sector-relative score weight to candidate ranking. If a proxy history file is missing, the symbol can still be evaluated by ordinary rules, but no sector-relative setup or rank boost is applied.

### Option Candidate Enrichment

`swing_strategy.py` emits `option_candidate` intent metadata for equity finalists when options are enabled. This is not an order. Before staging an option order candidate, the orchestrator must:

1. Confirm the Agentic account is `agentic_allowed=true` and has `option_level_2` or `option_level_3`; otherwise set option trading blocked and do not call option review/place tools.
2. Use `get_option_chains` and `get_option_instruments` for the underlying.
3. Select a single long call or long put contract matching configured DTE, delta, spread, open-interest, and volume constraints.
4. Use `get_option_quotes` and cap max loss at the full debit premium: `contracts * limit_price * 100`.
5. Stage an enriched pending candidate with `asset_type="option"`, `option_id`, `option_limit_price`, `contracts`, `option_type`, `option_strategy`, `max_loss`, and the original underlying symbol.

Option orders must be single-leg buy-to-open limit orders, regular-hours, GFD. Exits must be single-leg sell-to-close. No broker-side protective stop is required for long options; automated synthetic monitoring uses option quotes and sell-to-close actions.

### Automated Option Exit Monitoring

For every live monitor run, fetch option positions, option orders, and option quotes for open long-option positions. The monitor emits `review_and_place_option_sell_to_close` when any configured exit condition is met:

- Profit target: option quote is at least 50% above entry premium.
- Stop loss: option quote is at least 35% below entry premium.
- Time stop: days to expiration is 14 or fewer.
- Underlying stop: underlying price is at or below the stored `underlying_stop_price`.

Do not submit a duplicate sell-to-close if an open close order already exists for the same `option_id`. If option quotes are missing or unusable, do not trade and report the data condition. If the Agentic account lacks required option approval, block option exits and notify; continue equity monitoring.

### Historical Price CSV Phase

For every candidate-generation run, `swing_strategy.py` requires one fresh daily OHLC CSV per configured symbol. Treat `outputs/strategy_config.json:data_freshness.refresh_price_history_before_candidate_scan=true` as a hard pre-scan rule: refresh the full configured universe before scanning so expanded symbols are evaluated instead of skipped for missing or stale `SYMBOL.csv` files.

```bash
python3 outputs/refresh_price_history.py \
  --config outputs/strategy_config.json \
  --output-dir work/agentic_price_history \
  --raw-dir work/agentic_price_history_raw
```

This public-history refresh writes only local daily `Date,Open,High,Low,Close` files. It must not be used for live entry validation; continue to use Robinhood `get_equity_quotes` for live prices and max-entry checks. After refresh, verify every non-excluded universe symbol has at least the configured minimum daily bars and that the latest bar is no older than `data_freshness.max_history_age_calendar_days`, allowing weekends and market holidays.

If public history is unavailable or stale, use the broker-consistent fallback:

1. Call Robinhood `get_equity_historicals` with `interval=day`, `bounds=regular`, `adjustment_type=split`, and a start date at least 260 calendar days before the scan.
2. Fetch symbols in small batches of 10 or fewer. If Codex output size becomes difficult to inspect, reduce to 3-5 symbols per batch. Do not skip the benchmark `SPY`.
3. Save each raw historical JSON response under `work/agentic_price_history_raw/`.
4. Convert the saved responses into scanner input:

```bash
python3 outputs/robinhood_historicals_to_prices.py \
  work/agentic_price_history_raw/*.json \
  --output-dir work/agentic_price_history
```

5. Run `swing_strategy.py --prices-dir work/agentic_price_history ...`.

Both converters write only scanner OHLCV columns and skip any symbol with fewer than the configured minimum real daily bars. If a symbol's technical history is still missing or stale after the public refresh and broker fallback, mark that symbol ineligible and report the data condition; do not silently skip it. SPY/QQQ/VIX remain context and ranking inputs when `strategy.market_regime_filter.enabled=false`; missing benchmark data should block only calculations that directly require that benchmark, not all new-buy candidates.

### Monitor Phase

Run `agentic_monitor.py` with the normalized snapshots and any pending candidates. It returns:

- `events`: meaningful changes to report.
- `actions`: broker actions to execute.
- `next_poll_seconds`: `3600` or `null`.
- `daily_brief`: compact after-close summary.
- `state`: updated local strategy state.

After each non-`--no-save` monitor run, also write:

```bash
python3 outputs/agentic_monitor.py \
  --config outputs/strategy_config.json \
  --state work/agentic_live_adapter_state.json \
  --account-json ACCOUNT_SNAPSHOT_JSON \
  --orders-json ORDERS_SNAPSHOT_JSON \
  --quotes-json QUOTES_SNAPSHOT_JSON \
  --candidates-json CANDIDATES_JSON \
  --mode position \
  --tasks-json work/agentic_tasks.json \
  --tasks-md work/agentic_tasks.md
```

The task queue is derived from current account/order/quote snapshots. It is not a separate execution engine. Use it to make queued work explicit:

- `queued_buy_monitor`: open or queued buy orders that need fill/expire/cancel reconciliation. Do not arm a protective stop until a buy has a confirmed fill.
- `protective_stop_check`: every stock position must have exactly one broker-side protective stop for the full current position quantity.
- `profit_target_check`: watch synthetic target and partial/full exit conditions.
- `stop_ratchet_check`: evaluate whether a stop can be raised; never lower an existing stop.
- `option_exit_check`: monitor long-option profit target, stop loss, time stop, and underlying stop conditions.

Any task with `status=risk` is a monitor event and should be surfaced under the normal notification policy.

Before evaluating pending buys, stop ratchets, profit targets, or option exits, reconcile local state from current broker positions and open orders. Broker state is authoritative for current quantity, open stop IDs, buying power, and whether a candidate is still eligible. If local state disagrees with broker state, update local state from the broker snapshot before making a trading decision.

### Execution Phase

For each action:

- `review_and_place_equity_buy`
  - Call `review_equity_order`.
  - Confirm the order will not breach the configured cash reserve, total open-risk cap, max position exposure, or add-on aggregate exposure cap.
  - If broker review passes without blocking alerts, call `place_equity_order`.
  - Use a fresh idempotency `ref_id`.
  - On fill, call `handle_buy_fill` through the monitor flow, then place the protective stop.

- `review_and_place_option_buy_to_open`
  - Confirm the Agentic account still has required option approval immediately before review.
  - Call `review_option_order` with one buy/open leg, `type=limit`, `time_in_force=gfd`, and `market_hours=regular_hours`.
  - Surface all review alerts. If any alert blocks the order, do not place it.
  - If review passes, call `place_option_order` with the same parameters and a fresh `ref_id`.
  - Save the option order ID and option position metadata in private state.

- `review_and_place_option_sell_to_close`
  - Call `review_option_order` with one sell/close leg.
  - If review passes, call `place_option_order` with a fresh `ref_id`.
  - Update option position state after fills.

- `cancel_option_order`
  - Resolve the option order ID through `get_option_orders`.
  - Call `cancel_option_order` only for open option orders belonging to the Agentic account.

- `place_protective_stop`
  - If `execution.consolidate_protective_stops_by_symbol=true`, maintain exactly one open broker-side protective stop per equity symbol.
  - The stop quantity must match the full current broker position quantity, not only the latest fill quantity.
  - For an add-on fill, resolve all open stop-market sell orders for that symbol, cancel them, then place one replacement GTC stop-market sell for the full current broker position quantity.
  - Use the proposed stop price unless that would lower an existing stop; when consolidating existing stops, preserve the highest current stop price or the proposed stop price, whichever is higher.
  - Call `review_equity_order` with `side=sell`, `type=stop_market`, `stop_price`, and `time_in_force=gtc`.
  - If accepted, call `place_equity_order`.
  - Save the single consolidated stop order ID in private state.
  - If rejected, pause new buys and notify immediately.

### Trade Ledger

Append trade lifecycle records to `work/agentic_trade_ledger.jsonl`. The ledger is the source for strategy feedback, not chat history. Do not write full account numbers, raw broker account objects, or sensitive identifiers. Include symbol, setup type, entry, stop, target, shares, score, news score, fills, exits, realized P/L, R multiple, and candidate rejection reason when available. Weekly reviews should summarize win rate, average win/loss, expectancy by setup type, and loss cause.

- `cancel_or_reduce_protective_stop`
  - Resolve current protective stop order ID from state or open orders.
  - Call `cancel_equity_order` if a conflicting stop must be canceled.
  - Never place an independent full-quantity target while a full-quantity stop is live unless native linked OCO exists.

- `place_profit_limit_sell`
  - Call `review_equity_order` with `side=sell`, `type=limit`, and the target limit price.
  - Call `place_equity_order` only after review passes.
  - Save profit order ID and update state after fill.

- `replace_stop_for_remaining_quantity`
  - Recompute remaining quantity after profit fill.
  - Place a new protective stop for remaining shares.
  - If replacement fails, pause new buys and notify immediately.

- `raise_or_replace_protective_stop`
  - Use only when the monitor proposes a higher `new_stop_price`; never lower an existing protective stop.
  - Resolve the current broker-side stop from `protective_stop_order_id` or open stop orders for the symbol.
  - If more than one stop exists for the symbol, cancel all symbol stops and replace them with one full-position stop.
  - Call `cancel_equity_order` for the old stop or stops, then call `review_equity_order` and `place_equity_order` for a new GTC stop-market sell at `new_stop_price`.
  - Keep the new stop at least the configured minimum distance below current price and require the configured minimum raise over the old stop.
  - If cancel or replacement fails, pause new buys, keep monitoring the exposed position, and notify immediately.

## Adaptive Schedule

Use one thread heartbeat for the live orchestrator:

- Candidate scans: separate cron automation at 6:00 AM PT, 10:00 AM PT, and 5:00 PM PT on weekdays. All candidate-scan runs may create/update pending candidates only and must not review, place, cancel, or modify orders.
- Premarket candidate scan: 6:00 AM PT, 30 minutes before regular market open.
- Intraday candidate scan: 10:00 AM PT, using current account constraints, held symbols, live prices, and open-risk budget.
- After-close candidate scan: 5:00 PM PT, candidate-only. It must refresh full-universe daily OHLC history and may update pending candidates for next-session validation, but it must not perform broker order actions.
- Schedule mode: regular market hours only.
- Regular market window: 6:30 AM to 1:00 PM PT, Monday through Friday, excluding market holidays.
- Current single-heartbeat envelope: weekdays at 6:00, 7:00, 8:00, 9:00, 10:00, 11:00, 12:00, 1:00, and 5:00 PT. The 6:00 and 5:00 runs are candidate-only; the 10:00 run may combine live monitoring and candidate rediscovery if the slot has not already been recorded.
- Normal cadence: every 1 hour during the regular market window when a position is open or pending candidate requires validation.
- Elevated states: keep the 1-hour heartbeat when an order is active, first 30 minutes after entry, or price is within 1% of stop/target; live monitor runs still notify every time, with extra detail only on meaningful state changes.
- Morning validation: run at or after 7:00 AM PT / 10:00 AM ET when a pending candidate exists.
- After-close scan/brief: 5:00 PM PT weekdays, candidate-only. It may refresh history and update pending candidates for the next session, but it must not place, cancel, or modify broker orders.
- Outside market windows: no-op unless an order/position risk event is unresolved.

The orchestrator should keep the heartbeat cadence at 1 hour. Do not request a one-minute or 15-minute cadence unless the user explicitly changes the schedule; elevated states are handled by the same hourly market-hours run plus event notifications.

Every live monitor run must send a concise chat status, even when there are no actions or meaningful events. The no-action status should include the slot, account status, active positions, protective-stop status, pending candidates count, and next scheduled check. Candidate-only scans may stay quiet unless they stage candidates, hit a data/tool/risk condition, or otherwise need attention.

## Notifications

Always notify after every live monitor run, including ok/no-action checks. In addition, notify on these events:

- Candidate skipped or actionable.
- Buy submitted, filled, rejected, canceled, or partially filled.
- Protective stop submitted, active, rejected, canceled, or replaced.
- Profit target reached and dynamic action selected.
- Profit sell submitted, filled, rejected, or canceled.
- Manual activity detected.
- Daily loss stop reached.
- Daily after-close brief.

## Failure Policy

- Missing Robinhood tools: pause live trading and notify once.
- Stale/missing quotes: do not trade.
- Broker review warning that blocks order: do not place order.
- Manual activity: pause new buys and reconcile.
- State mismatch: pause new buys until resolved.
- Any protective stop failure: pause new buys immediately.
