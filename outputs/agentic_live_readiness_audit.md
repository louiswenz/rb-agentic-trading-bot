# Agentic Live Trading Readiness Audit

Date: 2026-06-20

## Implemented

- Strategy configuration for automatic trading is present in `strategy_config.json`.
- Live monitor/orchestrator logic is present in `agentic_monitor.py`.
- Broker adapter boundary is documented in `broker_adapters.py`.
- Codex live execution runbook is present in `codex_robinhood_adapter_runbook.md`.
- Codex orchestrator prompt is present in `codex_live_orchestrator_prompt.md`.
- Private live state file exists at `work/agentic_live_adapter_state.json`.
- Main automation `daily-agentic-account-scan` is active.
- Normal heartbeat is every hour.
- Elevated order/risk states stay on the hourly heartbeat and notify only on meaningful state changes.
- Duplicate morning automation is paused.
- Local readiness verifier passes.
- Behavior tests for monitor decisions are present in `test_agentic_monitor.py`.
- Robinhood MCP login succeeded.
- Robinhood MCP tool approvals are configured for required read-only snapshot tools, equity review/place/cancel tools, and must include option review/place/cancel tools before live option trading can be enabled.

## Trading Rules Confirmed

- Maximum automatic new buys per day: 2.
- Normal open-position monitoring: 3600 seconds.
- Elevated monitoring: 3600 seconds.
- Minimum new-buy reward/risk: 1.5R.
- Total open-risk cap: 6% of account value.
- Sector/group concentration cap is disabled.
- Long option support is approval-gated; live option trading remains blocked unless the Agentic account reports option level 2 or 3.
- Long option exits are automated with premium target, premium stop, DTE time stop, and underlying-stop triggers.
- Earnings blackout is enabled when earnings timing is supplied.
- Protective broker-side stop required after buy fill.
- R-based synthetic profit target tracked by monitor.
- Profit-target sells do not count against the daily new-buy cap.
- New buys pause on manual activity, daily loss stop, or protective stop failure.

## Live Execution Status

Robinhood MCP is configured, login completed, and a fresh read-only Codex run successfully used Robinhood MCP tools.

- Confirmed account: Agentic cash account ending `6332`.
- Account state: active.
- Agentic allowed: true.
- Buying power: `$2,000.0000 USD`.
- Account/portfolio value: `$2,000`.
- Equity value: `$0`.
- Equity positions count: `0`.
- Option positions count: `0`.
- Open equity orders count: `0`.

The current chat thread still does not directly expose Robinhood tools through its active tool list, but fresh Codex runs and automations can access the authenticated Robinhood MCP server. The scheduled orchestrator remains configured to pause live trading and notify if Robinhood tools or required broker data are unavailable at runtime. It must not invent account, quote, position, or order state.

## Evidence Checked

- `outputs/verify_agentic_setup.py` passes.
- `python3 -m unittest outputs/test_agentic_monitor.py` passes six behavior tests covering auto-buy creation, daily buy cap enforcement, buy-fill exit arming, target-trigger profit actions, normal/elevated polling, and manual activity pause.
- Python syntax compilation passes for the trading scripts.
- Automation `daily-agentic-account-scan` is active with `FREQ=MINUTELY;INTERVAL=15`.
- Automation `agentic-morning-bot-run` is paused to prevent duplicate trading.
- `codex mcp login robinhood` completed successfully.
- Fresh read-only `codex exec` run confirmed Robinhood MCP tools were available.
- `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_option_positions`, and `get_equity_orders` succeeded in the fresh read-only verification run.
- No order review, placement, cancellation, or modification tools were called during verification.

## Completion Status

The local strategy, monitor, runbook, automation schedule, Robinhood MCP login, and read-only broker handshake are implemented and verified. Live trading remains governed by the configured hard caps, equity and approval-gated long-option scope, broker-side protective stop requirement for equity positions, synthetic profit target workflow, and runtime pause policy for missing broker data or failed stop replacement.
