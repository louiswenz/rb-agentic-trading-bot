# Codex-Orchestrated Robinhood Adapter Runbook

This runbook defines the live adapter layer for the Agentic trading strategy. The local Python scripts produce state, events, and action requests. Codex automations call Robinhood tools, feed snapshots into `agentic_monitor.py`, execute returned actions, save state, and notify the user.

## Standing Authorization

Live automation is authorized only within these hard caps:

- Agentic account only.
- Max 2 automatic new buys per trading day.
- Under $5,000 account value: max 1 open position.
- Max 50% account exposure per position.
- Max 4% planned risk per trade.
- Pause new buys at 5% daily drawdown from start-of-day equity.
- Limit buy orders only.
- Stop-market GTC protective sell after every buy fill.
- Synthetic profit target only unless native linked OCO/bracket support is available.
- No options, crypto, futures, margin, shorts, market buys, leveraged ETFs, or unsettled-cash reuse.

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
- `get_equity_quotes`: live quotes for active position, pending candidates, `SPY`, and tradeable symbols.
- `get_equity_historicals`: daily regular-hours OHLCV bars for the benchmark and trade-universe symbols.
- Latest stock news snapshot for each trade-universe symbol when available from connected tools or approved web/news sources.

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

For daily candidate generation, pass the news snapshot into `swing_strategy.py` with `--news-json`. The snapshot is keyed by symbol and may include:

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

### Historical Price CSV Phase

For premarket candidate generation, `swing_strategy.py` requires one daily OHLC CSV per symbol. Refresh the configured universe before scanning so expanded symbols are evaluated instead of skipped for missing `SYMBOL.csv` files:

```bash
python3 outputs/refresh_price_history.py \
  --config outputs/strategy_config.json \
  --output-dir work/agentic_price_history \
  --raw-dir work/agentic_price_history_raw
```

This public-history refresh writes only local daily `Date,Open,High,Low,Close` files. It must not be used for live entry validation; continue to use Robinhood `get_equity_quotes` for live prices and max-entry checks.

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

Both converters write only `Date,Open,High,Low,Close` and skip any symbol with fewer than 201 real daily bars. If required technical history is still missing after the refresh and fallback, create no candidates; missing data remains a no-trade condition.

### Monitor Phase

Run `agentic_monitor.py` with the normalized snapshots and any pending candidates. It returns:

- `events`: meaningful changes to report.
- `actions`: broker actions to execute.
- `next_poll_seconds`: `900`, `60`, or `null`.
- `daily_brief`: compact after-close summary.
- `state`: updated local strategy state.

### Execution Phase

For each action:

- `review_and_place_equity_buy`
  - Call `review_equity_order`.
  - If broker review passes without blocking alerts, call `place_equity_order`.
  - Use a fresh idempotency `ref_id`.
  - On fill, call `handle_buy_fill` through the monitor flow, then place the protective stop.

- `place_protective_stop`
  - Call `review_equity_order` with `side=sell`, `type=stop_market`, `stop_price`, and `time_in_force=gtc`.
  - If accepted, call `place_equity_order`.
  - Save the stop order ID in private state.
  - If rejected, pause new buys and notify immediately.

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

## Adaptive Schedule

Use one thread heartbeat for the live orchestrator:

- Premarket candidate scan: separate cron automation at 6:00 AM PT, 30 minutes before regular market open. It may create/update pending candidates only and must not review, place, cancel, or modify orders.
- Schedule mode: regular market hours only.
- Regular market window: 6:30 AM to 1:00 PM PT, Monday through Friday, excluding market holidays.
- Current single-heartbeat envelope: weekdays at 15-minute marks from 6:00 AM through 1:45 PM PT, with mandatory quiet no-op at 6:00, 6:15, 1:15, 1:30, and 1:45 unless unresolved protective-stop or open-order risk exists. This envelope is used because this thread supports only one active heartbeat automation.
- Normal cadence: every 15 minutes during the regular market window when a position is open or pending candidate requires validation.
- Elevated cadence: every 1 minute when an order is active, first 30 minutes after entry, or price is within 1% of stop/target.
- Morning validation: run at or after 6:45 AM PT / 9:45 AM ET when a pending candidate exists.
- After-close scan/brief: not scheduled while market-hours-only mode is active. Add a separate after-close automation if you want candidate generation and daily brief outside regular hours.
- Outside market windows: no-op unless an order/position risk event is unresolved.

The orchestrator should update its heartbeat cadence based on `next_poll_seconds` when supported. If cadence mutation is unavailable in a run, keep the 15-minute heartbeat and report that elevated 1-minute monitoring was requested but not applied.

## Notifications

Notify only on events:

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
