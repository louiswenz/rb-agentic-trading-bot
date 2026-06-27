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
| Pending candidate | 7:00 AM PT next session | Notify only if actionable |
| Open position | Every hour during regular hours | Silent unless event |
| Active order | Every hour | Notify on state changes |
| Within 1% of stop or target | Every hour | Notify on risk/action events |
| After-close candidate picking | 5:00 PM PT weekdays | Candidate-only; no broker order actions |

Candidate picking runs as a separate cron automation at 6:00 AM, 10:00 AM, and 5:00 PM PT on weekdays. It can create or update pending candidates only; it must not submit, review, cancel, or modify orders. Candidate discovery uses deterministic technical/risk prescreening before collecting news for the smaller survivor set.

The Codex live adapter uses one hourly heartbeat for market-hours monitoring and execution. The current schedule is regular-market-hours-only: 6:30 AM to 1:00 PM PT on weekdays, excluding market holidays. The saved heartbeat runs at 6:00, 7:00, 8:00, 9:00, 10:00, 11:00, 12:00, 1:00, and 5:00 PT. The 6:00 and 5:00 runs are candidate-only; elevated order/stop/target states still use the same hourly heartbeat and notify only on meaningful state changes.

Live adapter state lives at `work/agentic_live_adapter_state.json`. It uses the Agentic account selector and standing authorization scope; the full account number is resolved by Codex via Robinhood tools and must remain out of user-facing output.

## Monitor Inputs

`agentic_monitor.py` expects:

- `--account-json`: account snapshot with equity, buying power, and positions.
- `--orders-json`: current order snapshot.
- `--quotes-json`: live quote snapshot for active positions, pending candidates, `SPY`, `QQQ`, `VIX`, and required risk/context symbols.
- `--candidates-json`: pending candidates from the scanner.
- `--state`: persistent monitor state file.

## Monitor Outputs

- `events`: meaningful changes worth notifying or recording.
- `actions`: broker actions a Robinhood adapter should review/submit.
- `next_poll_seconds`: `3600` or `null`.
- `daily_brief`: compact after-close summary when scheduled separately.
- `state`: updated persistent state.

## Safety Behavior

- Daily loss stop pauses new buys at 5% drawdown from start-of-day equity.
- New buys are skipped when they would exceed the 6% total open-risk cap.
- New buys are skipped when they violate the configured sector/group cap.
- New buys are skipped below the 1.5R minimum reward/risk.
- Manual activity pauses new buys until account state is reconciled.
- Protective stop failure pauses new buys.
- Profit target execution never places an independent full-quantity target while a full-quantity stop is live unless native linked OCO support exists.
- Soft daily refinements cannot increase hard risk caps.
