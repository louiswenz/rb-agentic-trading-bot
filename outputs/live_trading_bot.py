#!/usr/bin/env python3
"""Live-bot runner for the Agentic strategy.

Default mode is paper/mock. This runner never places real orders unless a real
BrokerAdapter is provided and selected.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from agentic_monitor import (
    handle_buy_fill,
    handle_exit_fill,
    load_json,
    load_state,
    run_monitor,
    save_state,
)
from broker_adapters import MockBrokerAdapter, RobinhoodAdapter


SENSITIVE_OUTPUT_KEYS = {
    "account_id",
    "account_number",
    "agentic_account_number",
    "cancel_existing_order_ids",
    "id",
    "last_equity_order_ids",
    "last_ref_ids",
    "order_id",
    "order_ids",
    "profit_order_id",
    "protective_stop_order_id",
    "ref_id",
    "ref_ids",
}


def sanitize_for_output(value: Any) -> Any:
    """Redact broker/account identifiers from console JSON."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in SENSITIVE_OUTPUT_KEYS:
                if isinstance(item, list):
                    redacted[key] = f"[REDACTED_LIST:{len(item)}]"
                elif item is None:
                    redacted[key] = None
                else:
                    redacted[key] = "[REDACTED]"
            else:
                redacted[key] = sanitize_for_output(item)
        return redacted
    if isinstance(value, list):
        return [sanitize_for_output(item) for item in value]
    return value


def watched_symbols(state: dict[str, Any], candidates: list[dict[str, Any]], config: dict[str, Any]) -> list[str]:
    symbols = [config["strategy"]["benchmark_symbol"], "VIX"]
    if state.get("position"):
        symbols.append(state["position"]["symbol"])
    for position in state.get("option_positions", []) or []:
        if position.get("option_id"):
            symbols.append(position["option_id"])
        if position.get("symbol"):
            symbols.append(position["symbol"])
    symbols.extend(candidate["symbol"] for candidate in candidates)
    return sorted(set(symbols))


def choose_mode(state: dict[str, Any], candidates: list[dict[str, Any]], requested: str) -> str:
    if requested != "auto":
        return requested
    if state.get("position"):
        return "position"
    if state.get("option_positions"):
        return "position"
    if candidates:
        return "pending"
    return "daily"


def build_adapter(kind: str, mock_path: Path, persist: bool = True):
    if kind == "mock":
        return MockBrokerAdapter(mock_path, persist=persist)
    if kind == "robinhood":
        return RobinhoodAdapter()
    raise ValueError(f"Unsupported adapter: {kind}")


def execute_actions(adapter: Any, state: dict[str, Any], config: dict[str, Any], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for action in actions:
        result = adapter.execute_action(action)
        results.append({"action": action, "result": result})

        if action["type"] == "review_and_place_equity_buy" and result.get("status") == "filled":
            fill_events, followup_actions = handle_buy_fill(state, result["fill"], config)
            results.append({"events": fill_events})
            for followup in followup_actions:
                followup_result = adapter.execute_action(followup)
                results.append({"action": followup, "result": followup_result})

        if action["type"] == "place_profit_limit_sell" and result.get("status") == "filled":
            exit_events = handle_exit_fill(state, result["fill"])
            results.append({"events": exit_events})
    return results


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    base_dir = Path(__file__).resolve().parent
    config_path = Path(args.config)
    if not config_path.exists() and args.config == "strategy_config.json":
        config_path = base_dir / "strategy_config.json"
    config = load_json(str(config_path), {})

    state_path = Path(args.state)
    state = load_state(state_path)
    candidates = load_json(args.candidates_json, [])
    mode = choose_mode(state, candidates, args.mode)

    adapter = build_adapter(args.adapter, Path(args.mock_broker_state), persist=not args.no_save)
    account = adapter.get_account_snapshot()
    orders = adapter.get_orders_snapshot()
    quotes = adapter.get_quotes(watched_symbols(state, candidates, config))

    result = run_monitor(
        state=state,
        config=config,
        account=account,
        orders=orders,
        quotes=quotes,
        candidates=candidates,
        mode=mode,
        trading_date=args.trading_date,
    )
    execution_results = [] if args.dry_run else execute_actions(adapter, result.state, config, result.actions)
    if not args.no_save:
        save_state(state_path, result.state)

    return {
        "mode": mode,
        "adapter": args.adapter,
        "dry_run": args.dry_run,
        "events": result.events,
        "actions": result.actions,
        "execution_results": execution_results,
        "next_poll_seconds": result.next_poll_seconds,
        "daily_brief": result.daily_brief,
        "state": result.state,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Agentic live trading bot.")
    parser.add_argument("--adapter", choices=["mock", "robinhood"], default="mock")
    parser.add_argument("--config", default="strategy_config.json")
    parser.add_argument("--state", default="outputs/agentic_state.json")
    parser.add_argument("--mock-broker-state", default="outputs/mock_broker_state.json")
    parser.add_argument("--candidates-json", default="[]", help="Candidate list JSON or path.")
    parser.add_argument("--mode", choices=["auto", "pending", "position", "daily"], default="auto")
    parser.add_argument("--trading-date", default=datetime.now().date().isoformat())
    parser.add_argument("--loop", action="store_true", help="Keep polling using monitor next_poll_seconds.")
    parser.add_argument("--dry-run", action="store_true", help="Emit actions but do not execute them through adapter.")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=1)
    args = parser.parse_args()

    outputs: list[dict[str, Any]] = []
    iterations = 0
    while True:
        payload = run_once(args)
        outputs.append(payload)
        print(json.dumps(sanitize_for_output(payload), indent=2))
        iterations += 1
        if not args.loop or iterations >= args.max_iterations:
            break
        sleep_seconds = payload.get("next_poll_seconds") or 3600
        time.sleep(float(sleep_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
