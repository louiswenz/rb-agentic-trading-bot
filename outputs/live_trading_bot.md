# Agentic Live Trading Bot

`live_trading_bot.py` is the runner around the strategy monitor.

Current status:

- Paper/mock trading is implemented.
- Real Robinhood execution is intentionally not wired in this local script.
- Live Robinhood execution is handled by Codex-orchestrated automations using the runbook in `codex_robinhood_adapter_runbook.md`.
- A future non-Codex broker adapter may provide account snapshots, order snapshots, live quotes, and order execution through an official API.

## Safe Paper Run

```bash
python3 outputs/live_trading_bot.py \
  --adapter mock \
  --mode pending \
  --candidates-json '[{"symbol":"QQQ","shares":6,"max_next_session_entry":139.11}]'
```

## Dry Run

```bash
python3 outputs/live_trading_bot.py \
  --adapter mock \
  --mode pending \
  --dry-run \
  --candidates-json '[{"symbol":"QQQ","shares":6,"max_next_session_entry":139.11}]'
```

## Live Broker Requirements

A non-Codex real broker adapter would need to implement:

- `get_account_snapshot()`
- `get_orders_snapshot()`
- `get_quotes(symbols)`
- `execute_action(action)`

The adapter must call broker-side review/simulation before any order and must honor:

- Max 2 automatic new buys per day.
- 50% max position size.
- 4% max planned risk.
- 5% daily loss stop.
- One open position under $5k.
- Protective stop after every fill.
- Protective stop ratchet: breakeven after 0.75R, then 8% trailing from the high-water mark after 1R, never lowering stops.
- Synthetic target guardrails.
- Manual activity pause.

Do not run local Python with a real adapter until paper runs match expected behavior. For the Agentic Robinhood account, prefer the Codex-orchestrated adapter because Robinhood tools are available to Codex, not directly to local Python.
