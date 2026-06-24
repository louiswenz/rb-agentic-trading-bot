# Agentic Monitoring Operations

The automation is split into two local components:

- `swing_strategy.py` scans historical daily OHLC data and emits pending trade candidates.
- `agentic_monitor.py` consumes account, order, quote, and candidate snapshots and emits events, action requests, next polling interval, and daily briefs.
- `live_trading_bot.py` runs the monitor against a paper broker adapter and can execute returned actions in mock mode.
- `broker_adapters.py` provides a paper/mock adapter and a placeholder for real broker wiring.
- `codex_robinhood_adapter_runbook.md` defines the live Codex-orchestrated Robinhood adapter.

The local scripts do not directly submit Robinhood orders. Live trading is handled by Codex automations that call Robinhood tools, normalize snapshots, invoke `agentic_monitor.py`, execute returned actions, save state, and notify on events.

## Frequencies

| State | Frequency | User output |
|---|---:|---|
| Premarket candidate picking | 6:00 AM PT weekdays | Notify only if actionable candidates or failures |
| No position / no candidate | Market-hours heartbeat only | Quiet no-op unless event |
| Pending candidate | 6:45 AM PT next session | Notify only if actionable |
| Open position | Every 15 minutes during regular hours | Silent unless event |
| Active order | Every 1 minute | Notify on state changes |
| Within 1% of stop or target | Every 1 minute | Notify on risk/action events |
| Daily brief | 1:30 PM PT after close | Compact table |

Premarket candidate picking runs as a separate cron automation at 6:00 AM PT on weekdays, 30 minutes before regular market open. It can create or update pending candidates only; it must not submit, review, cancel, or modify orders.

The Codex live adapter uses one adaptive heartbeat for market-hours monitoring and execution. The current schedule is regular-market-hours-only: 6:30 AM to 1:00 PM PT on weekdays, excluding market holidays. Because this thread supports only one active heartbeat automation, the saved schedule uses a 15-minute market-hours envelope from 6:00 AM through 1:45 PM PT and the prompt must quietly no-op at 6:00, 6:15, 1:15, 1:30, and 1:45 unless unresolved protective-stop or open-order risk exists. The heartbeat should move to 1 minute during elevated conditions when supported, then return to the market-hours schedule when the condition clears.

Live adapter state lives at `work/agentic_live_adapter_state.json`. It uses the Agentic account selector and standing authorization scope; the full account number is resolved by Codex via Robinhood tools and must remain out of user-facing output.

## Monitor Inputs

`agentic_monitor.py` expects:

- `--account-json`: account snapshot with equity, buying power, and positions.
- `--orders-json`: current order snapshot.
- `--quotes-json`: live quote snapshot for position, pending candidates, `SPY`, and `VIX`.
- `--candidates-json`: pending candidates from the scanner.
- `--state`: persistent monitor state file.

## Monitor Outputs

- `events`: meaningful changes worth notifying or recording.
- `actions`: broker actions a Robinhood adapter should review/submit.
- `next_poll_seconds`: `900`, `60`, or `null`.
- `daily_brief`: compact after-close summary only if a separate after-close run is added later.
- `state`: updated persistent state.

## Safety Behavior

- Daily loss stop pauses new buys at 5% drawdown from start-of-day equity.
- Manual activity pauses new buys until account state is reconciled.
- Protective stop failure pauses new buys.
- Profit target execution never places an independent full-quantity target while a full-quantity stop is live unless native linked OCO support exists.
- Soft daily refinements cannot increase hard risk caps.
