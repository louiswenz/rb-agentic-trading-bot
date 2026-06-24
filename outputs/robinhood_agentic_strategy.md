# Robinhood Agentic Swing Trading Rulebook

This rulebook implements a concentrated, higher-risk, rules-based swing strategy for the Robinhood Agentic cash account ending in 6332. It is designed for bounded automatic trading with event-driven notifications. It is not personalized financial advice.

## Account Setup

- Account: Robinhood Agentic, individual cash account, account ending 6332.
- Current state at setup: active, agentic trading enabled, no buying power, no positions, no options approval.
- Strategy activation: inactive until funded with settled cash.
- Current working capital assumption: about $2,000 buying power.
- Expected funding range for full strategy: $5,000 to $25,000.
- Execution mode: automatic trading with notifications, broker-side review/simulation where available, and hard risk caps.

## Trade Universe

Default symbols:

`SPY`, `QQQ`, `IWM`, `DIA`, `XLK`, `XLF`, `XLE`, `XLV`, `AAPL`, `MSFT`, `NVDA`, `AMD`, `AMZN`, `GOOGL`, `META`, `TSLA`, `SMCI`, `SPCX`, `SPCH`, `AVGO`, `JPM`, `LLY`, `COST`, `NFLX`

Market-risk indicators:

`VIX`

Rules:

- Long-only equities and ETFs.
- No options, crypto, futures, margin, short selling, or leveraged ETFs in v1.
- `VIX` is an informational volatility indicator only, not a buy candidate.
- Regular-hours trading only.
- Current automation schedule is regular-market-hours-only. After-close scans create pending candidates only if a separate after-close automation is enabled later.
- Prefer limit orders near the current quote over plain market orders.
- Every automatic buy must include a broker-side protective stop-loss sell after the buy fills.
- Every automated trade has both downside protection and a profit-target plan: broker-side stop-loss plus a synthetic monitored target.
- Maximum automatic new buys per day: 2.

## Daily Scan

The current automation picks candidates 30 minutes before regular market open at 6:00 AM PT on weekdays. This premarket scan uses the latest available historical OHLC data, account state, and stock-specific news. It may create pending candidates only; it must not submit, review, cancel, or modify orders.

1. Confirm `SPY` is above its 50-day moving average.
2. For each watchlist symbol, require:
   - Close above 50-day moving average.
   - Close above 200-day moving average.
   - 20-day relative strength versus `SPY` is positive.
   - Close is above the prior day high.
   - Same-day move is no more than 5%.
   - Recent pullback or consolidation is present.
3. Review latest stock-specific news from the last 48 hours when available.
4. Block a candidate on severe adverse news, including fraud, bankruptcy, delisting, trading halt, SEC/accounting investigation, or a configured adverse news score at or below -2.
5. Rank passing candidates by a combined score: 20-day relative strength plus the configured latest-news score weight.
6. Produce at most 2 pending trade candidates, with the strongest candidate preferred.
7. Assign each candidate a maximum acceptable next-session entry price, defaulting to 1.0% above the signal close.

News is a decision-support input, not a standalone reason to buy. A symbol must still pass every market, trend, entry, sizing, and cash-account rule before news can improve its rank.

## Next-Session Price Validation

Because the scan runs after the market close, the next regular session can open above or below the signal price. No order may be placed from stale after-close prices.

Before any Robinhood order review:

- Refresh the live regular-hours quote.
- Recalculate position size from the live entry price.
- Use a limit order at or below the maximum acceptable entry price.
- Skip the trade if the live price is above the maximum acceptable entry price.
- Skip the trade if the updated position size violates the 4% risk limit, 50% position cap, available settled cash, or maximum-position rules.
- Emit an audit notification for the buy decision and include protective stop-loss details: symbol, filled quantity, stop price, order type, and time in force.

## Risk Rules

- Risk at most 4% of account value per trade.
- Cap each position at 50% of account value.
- Hold at most 3 positions when account value is $5,000 to $25,000.
- If account value is under $5,000, hold at most 2 positions at a time.
- For a $2,000 account, the default maximum position is about $1,000 and the maximum planned loss is about $80.
- Initial stop is the lower of:
  - 8% below entry.
  - Recent 10-session swing low.
- Pause new trades if current equity is 10% or more below starting monthly equity.
- Pause new buys if current equity is 5% or more below the start-of-day equity.
- Resume only after manual review.

## Event-Driven Monitoring

- No position / no candidate: quiet during the market-hours heartbeat unless an event occurs.
- Premarket candidate picking: 6:00 AM PT on weekdays, pending candidates only.
- Pending candidate: one morning validation event at 6:45 AM PT / 9:45 AM ET if actionable.
- Open position: silent checks every 15 minutes during regular market hours.
- Active order, first 30 minutes after entry, or within 1% of stop/target: keep the 15-minute market-hours heartbeat and notify only on meaningful state changes.
- Daily after-close brief: not scheduled in market-hours-only mode.
- Polls stay quiet unless a meaningful event occurs.

## Exit Rules

- Stop-loss exits are automatic after the buy fills: place a broker-side GTC stop-market sell for the filled quantity at the planned stop price.
- If the buy partially fills, place the stop only for the filled quantity.
- If the protective stop order is rejected, do not open any additional trade and surface the rejection immediately.
- Arm a synthetic profit target after the buy fills, defaulting to +12% from entry.
- If native linked OCO/bracket support is available, prefer a native linked stop/target package.
- If native linked OCO/bracket support is not available, do not place an independent full-quantity profit target while a full-quantity stop is live.
- When the synthetic target triggers, re-check the position and stop order, then choose dynamically:
  - Sell partial if momentum remains strong.
  - Sell full if setup weakens, VIX risk rises, or market filter deteriorates.
  - Trail only if trend strength remains high and risk remains acceptable.
- Before a profit sell, cancel, reduce, or replace the existing protective stop as needed to avoid conflicting sell orders.
- After any partial profit sell, replace or resize the protective stop for the remaining shares.
- If stop cancel/replacement or target sell fails, pause new buys and notify immediately.
- Trail the remaining position using the tighter operational review of:
  - 8% trailing stop from the highest close since entry.
  - Close below the 20-day moving average.
- Profit-target sells do not count against the 2 automatic new buys/day cap.
- Existing position exits may still be reviewed when the market filter is off.

## Profit Target Notifications

Notify the user whenever:

- A target is armed.
- A target is reached.
- The dynamic action is chosen: partial sell, full sell, or trail-only.
- A stop cancel/reduce/replace attempt starts.
- A profit sell is submitted.
- A profit sell fills, rejects, or cancels.
- Replacement stop submission succeeds or fails.

The daily brief must include active target price, stop price, distance to target, distance to stop, and any dynamic adjustment selected for the next day.

## Cash Account Guardrails

- Use settled cash only.
- Avoid same-day round trips.
- Do not sell securities bought with unsettled proceeds.
- Do not suggest a buy when the trade would exceed available settled cash.

## Implementation Artifacts

- `strategy_config.json`: default account, universe, risk, and execution settings.
- `swing_strategy.py`: offline scanner that reads OHLC CSVs plus optional latest-news snapshots and emits pending reviewed trade candidates with next-session price limits, protective stops, and synthetic profit-target packages.
- `agentic_monitor.py`: event-driven state and action engine for account reconciliation, monitoring frequency, auto-action decisions, event notes, and daily briefs.
- `codex_robinhood_adapter_runbook.md`: live Codex-orchestrated adapter instructions for Robinhood tools, schedules, action mapping, and failure policy.

The local scripts do not directly connect to Robinhood. A broker adapter must pass account/order/quote snapshots into the monitor and submit any returned action only through Robinhood review/execution tools.
