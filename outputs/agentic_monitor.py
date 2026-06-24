#!/usr/bin/env python3
"""Event-driven monitor/orchestrator for the Agentic swing strategy.

This module does not connect to Robinhood or place orders by itself. A broker
adapter should provide account/order/quote/candidate snapshots and submit any
returned actions through Robinhood review/execution tools.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STATE: dict[str, Any] = {
    "trading_date": None,
    "status": "ready",
    "paused_reason": None,
    "daily_buy_count": 0,
    "start_of_day_equity": None,
    "last_account_snapshot": None,
    "position": None,
    "positions": [],
    "pending_candidates": [],
    "open_orders": [],
    "last_events": [],
    "strategy_overrides": {},
}


@dataclass(frozen=True)
class MonitorResult:
    state: dict[str, Any]
    events: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    next_poll_seconds: int | None
    daily_brief: str | None = None


def load_json(path_or_value: str | None, default: Any) -> Any:
    if not path_or_value:
        return deepcopy(default)
    stripped = path_or_value.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(path_or_value)
    path = Path(path_or_value)
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path_or_value)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(DEFAULT_STATE)
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    merged = deepcopy(DEFAULT_STATE)
    merged.update(state)
    return merged


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def event(kind: str, message: str, **details: Any) -> dict[str, Any]:
    return {"time": now_iso(), "kind": kind, "message": message, "details": details}


def reset_daily_counters(state: dict[str, Any], account: dict[str, Any], trading_date: str) -> list[dict[str, Any]]:
    if state.get("trading_date") == trading_date:
        return []
    state["trading_date"] = trading_date
    state["daily_buy_count"] = 0
    state["start_of_day_equity"] = float(account.get("equity", account.get("account_value", 0.0)))
    return [event("daily_reset", "Daily counters reset.", trading_date=trading_date)]


def reconcile_account(
    state: dict[str, Any],
    account: dict[str, Any],
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    prior = state.get("last_account_snapshot") or {}
    state["last_account_snapshot"] = account
    state["open_orders"] = [order for order in orders if order.get("state") in {"open", "queued", "confirmed"}]

    positions = account.get("positions", [])
    state["account_positions"] = positions
    sync_tracked_positions(state, positions)

    if prior and prior.get("positions") != account.get("positions"):
        events.append(event("account_position_change", "Position state changed.", positions=account.get("positions", [])))
    if prior and prior.get("buying_power") != account.get("buying_power"):
        events.append(event("buying_power_change", "Buying power changed.", buying_power=account.get("buying_power")))
    return events


def sync_tracked_positions(state: dict[str, Any], account_positions: list[dict[str, Any]]) -> None:
    by_symbol = {position.get("symbol"): position for position in account_positions if position.get("symbol")}
    tracked = tracked_positions(state)
    synced: list[dict[str, Any]] = []

    for position in tracked:
        symbol = position.get("symbol")
        account_position = by_symbol.get(symbol)
        if not account_position:
            continue
        merged = deepcopy(position)
        merged["quantity"] = account_position.get("quantity", merged.get("quantity"))
        merged["average_buy_price"] = account_position.get("average_buy_price", merged.get("average_buy_price"))
        synced.append(merged)

    tracked_symbols = {position.get("symbol") for position in synced}
    for symbol, account_position in by_symbol.items():
        if symbol not in tracked_symbols:
            synced.append(account_position)

    state["positions"] = synced
    state["position"] = synced[0] if synced else None


def tracked_positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    positions = state.get("positions")
    if isinstance(positions, list) and positions:
        return [position for position in positions if position]
    position = state.get("position")
    return [position] if position else []


def detect_manual_activity(state: dict[str, Any], account: dict[str, Any], orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manual_orders = [order for order in orders if order.get("source") == "manual" and order.get("id") not in state.get("known_manual_order_ids", [])]
    if not manual_orders:
        return []
    state.setdefault("known_manual_order_ids", []).extend(order.get("id") for order in manual_orders if order.get("id"))
    state["status"] = "paused"
    state["paused_reason"] = "manual_activity_detected"
    return [event("manual_activity", "Manual account activity detected; new buys paused.", orders=manual_orders)]


def daily_loss_guard(state: dict[str, Any], account: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    start = state.get("start_of_day_equity")
    equity = float(account.get("equity", account.get("account_value", 0.0)))
    if not start:
        return []
    drawdown_pct = ((float(start) - equity) / float(start)) * 100.0
    if drawdown_pct < float(config["execution"]["daily_loss_stop_pct"]):
        return []
    state["status"] = "paused"
    state["paused_reason"] = "daily_loss_stop"
    return [event("daily_loss_stop", "Daily loss stop reached; new buys paused.", drawdown_pct=round(drawdown_pct, 2))]


def fetch_live_quotes(symbols: list[str], quote_snapshot: dict[str, Any]) -> dict[str, Any]:
    return {symbol: quote_snapshot[symbol] for symbol in symbols if symbol in quote_snapshot}


def detect_price_events(
    state: dict[str, Any],
    quotes: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    positions = tracked_positions(state)
    if not positions:
        return []

    near_pct = float(config["monitoring"]["near_stop_or_target_pct"])
    events: list[dict[str, Any]] = []

    for position in positions:
        symbol = position["symbol"]
        quote = quotes.get(symbol)
        if not quote:
            events.append(event("stale_quote", "No live quote for active position.", symbol=symbol))
            continue

        price = float(quote["price"])
        stop = float(position.get("stop_price", 0.0))
        target = float(position.get("target_price", 0.0))

        if stop and price <= stop * (1.0 + near_pct / 100.0):
            events.append(event("near_stop", "Price is near protective stop.", symbol=symbol, price=price, stop=stop))
        if target and price >= target * (1.0 - near_pct / 100.0):
            events.append(event("near_target", "Price is near profit target.", symbol=symbol, price=price, target=target))
        if target and price >= target:
            events.append(event("target_reached", "Synthetic profit target reached.", symbol=symbol, price=price, target=target))
    return events


def max_open_positions(account: dict[str, Any], config: dict[str, Any]) -> int:
    risk = config["risk"]
    equity = float(account.get("equity", account.get("account_value", 0.0)))
    if equity < float(risk["funding_minimum_standard_usd"]):
        return int(risk["max_positions_under_5000"])
    return int(risk["max_positions_standard"])


def current_open_position_count(state: dict[str, Any], account: dict[str, Any]) -> int:
    positions = account.get("positions") or []
    if positions:
        return len(positions)
    return 1 if state.get("position") else 0


def open_position_symbols(state: dict[str, Any], account: dict[str, Any]) -> set[str]:
    symbols = {position.get("symbol") for position in account.get("positions", []) if position.get("symbol")}
    for state_position in tracked_positions(state):
        if state_position.get("symbol"):
            symbols.add(state_position["symbol"])
    return symbols


def symbol_group(symbol: str, config: dict[str, Any]) -> str:
    groups = config["strategy"].get("sector_concentration", {}).get("symbol_groups", {})
    return str(groups.get(symbol.upper(), "other"))


def open_group_counts(state: dict[str, Any], account: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for symbol in open_position_symbols(state, account):
        group = symbol_group(symbol, config)
        counts[group] = counts.get(group, 0) + 1
    return counts


def max_positions_per_group(account: dict[str, Any], config: dict[str, Any]) -> int:
    concentration = config["strategy"].get("sector_concentration", {})
    risk = config["risk"]
    equity = float(account.get("equity", account.get("account_value", 0.0)))
    if equity < float(risk["funding_minimum_standard_usd"]):
        return int(concentration.get("max_positions_per_group_under_5000", 1))
    return int(concentration.get("max_positions_per_group_standard", 1))


def position_risk_dollars(position: dict[str, Any]) -> float:
    quantity = float(position.get("quantity", 0.0))
    entry = float(position.get("entry_price", position.get("average_buy_price", 0.0)) or 0.0)
    stop = float(position.get("stop_price", 0.0) or 0.0)
    if quantity <= 0 or entry <= 0 or stop <= 0:
        return 0.0
    return max(0.0, (entry - stop) * quantity)


def planned_open_risk_dollars(state: dict[str, Any], account: dict[str, Any]) -> float:
    risks_by_symbol: dict[str, float] = {}
    for position in account.get("positions", []) or []:
        symbol = position.get("symbol")
        if symbol:
            risks_by_symbol[symbol] = position_risk_dollars(position)
    for state_position in tracked_positions(state):
        if state_position.get("symbol"):
            risks_by_symbol[state_position["symbol"]] = max(
                risks_by_symbol.get(state_position["symbol"], 0.0),
                position_risk_dollars(state_position),
            )
    return sum(risks_by_symbol.values())


def remaining_open_risk_dollars(state: dict[str, Any], account: dict[str, Any], config: dict[str, Any]) -> float:
    equity = float(account.get("equity", account.get("account_value", 0.0)))
    cap = equity * (float(config["risk"]["total_open_risk_pct"]) / 100.0)
    return cap - planned_open_risk_dollars(state, account)


def monitor_pending_candidates(
    state: dict[str, Any],
    account: dict[str, Any],
    quotes: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    if state.get("status") == "paused":
        return events, actions
    if current_open_position_count(state, account) >= max_open_positions(account, config):
        events.append(event("max_positions_reached", "Candidate buying skipped because max open positions is reached."))
        return events, actions
    if int(state.get("daily_buy_count", 0)) >= int(config["execution"]["max_auto_buys_per_day"]):
        return events, actions

    held_symbols = open_position_symbols(state, account)
    group_counts = open_group_counts(state, account, config)
    max_group_count = max_positions_per_group(account, config)
    remaining_risk = remaining_open_risk_dollars(state, account, config)
    for candidate in state.get("pending_candidates", []):
        symbol = candidate["symbol"]
        if symbol in held_symbols:
            events.append(event("candidate_already_held", "Candidate skipped because symbol is already held.", symbol=symbol))
            continue
        group = str(candidate.get("sector_group") or symbol_group(symbol, config))
        if (
            config["strategy"].get("sector_concentration", {}).get("enabled", False)
            and group_counts.get(group, 0) >= max_group_count
        ):
            events.append(event("candidate_group_cap", "Candidate skipped because sector group cap is reached.", symbol=symbol, group=group))
            continue
        if float(candidate.get("reward_risk_ratio", 0.0)) < float(config["strategy"]["min_reward_risk_ratio"]):
            events.append(event("candidate_reward_risk_skip", "Candidate skipped below minimum reward/risk.", symbol=symbol))
            continue
        if float(candidate.get("max_loss", 0.0)) > remaining_risk:
            events.append(event("candidate_open_risk_skip", "Candidate skipped because total open-risk cap would be exceeded.", symbol=symbol))
            continue
        quote = quotes.get(symbol)
        if not quote:
            events.append(event("candidate_no_quote", "Candidate skipped because no live quote was available.", symbol=symbol))
            continue
        live_price = float(quote["price"])
        if live_price > float(candidate["max_next_session_entry"]):
            events.append(event("candidate_gap_skip", "Candidate skipped above max entry.", symbol=symbol, live_price=live_price))
            continue
        action = review_and_place_auto_buy(state, account, candidate, live_price, config)
        actions.append(action)
        events.append(event("auto_buy_planned", "Automatic buy action prepared.", symbol=symbol, live_price=live_price))
        break
    return events, actions


def review_and_place_auto_buy(
    state: dict[str, Any],
    account: dict[str, Any],
    candidate: dict[str, Any],
    live_price: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    quantity = int(candidate["shares"])
    estimated_cost = quantity * live_price
    return {
        "type": "review_and_place_equity_buy",
        "symbol": candidate["symbol"],
        "quantity": quantity,
        "order_type": config["execution"]["default_order_type"],
        "limit_price": min(live_price, float(candidate["max_next_session_entry"])),
        "estimated_cost": round(estimated_cost, 2),
        "estimated_max_loss": round(float(candidate.get("max_loss", 0.0)), 2),
        "reward_risk_ratio": round(float(candidate.get("reward_risk_ratio", 0.0)), 2),
        "sector_group": candidate.get("sector_group") or symbol_group(candidate["symbol"], config),
        "requires_broker_review": bool(config["execution"]["require_broker_review_before_order"]),
        "after_fill": {
            "place_protective_stop": True,
            "arm_synthetic_target": True,
            "target_price": candidate.get("target_price"),
            "partial_target": candidate.get("partial_target"),
        },
    }


def handle_buy_fill(
    state: dict[str, Any],
    fill: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    symbol = fill["symbol"]
    quantity = int(fill["quantity"])
    entry = float(fill["price"])
    stop = float(fill["stop_price"])
    target = entry + ((entry - stop) * float(config["risk"]["synthetic_profit_target_r_multiple"]))
    partial_target = entry + ((entry - stop) * float(config["risk"]["partial_profit_r_multiple"]))

    state["daily_buy_count"] = int(state.get("daily_buy_count", 0)) + 1
    new_position = {
        "symbol": symbol,
        "quantity": quantity,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "partial_target_price": partial_target,
        "highest_price": entry,
        "opened_at": now_iso(),
    }
    positions = [item for item in tracked_positions(state) if item.get("symbol") != symbol]
    positions.append(new_position)
    state["positions"] = positions
    state["position"] = positions[0] if positions else None
    events = [event("buy_filled", "Buy filled; exits armed.", symbol=symbol, quantity=quantity, entry=entry)]
    actions = [
        place_protective_stop(symbol, quantity, stop, config),
        {
            "type": "arm_synthetic_profit_target",
            "symbol": symbol,
            "partial_target_price": round(partial_target, 2),
            "target_price": round(target, 2),
            "quantity": quantity,
        },
    ]
    return events, actions


def place_protective_stop(symbol: str, quantity: int, stop_price: float, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "place_protective_stop",
        "symbol": symbol,
        "side": "sell",
        "quantity": quantity,
        "order_type": config["execution"]["protective_stop_order_type"],
        "stop_price": round(stop_price, 2),
        "time_in_force": config["execution"]["protective_stop_time_in_force"],
    }


def handle_stop_failure(state: dict[str, Any], reason: str) -> list[dict[str, Any]]:
    state["status"] = "paused"
    state["paused_reason"] = "protective_stop_failure"
    return [event("protective_stop_failure", "Protective stop failed; new buys paused.", reason=reason)]


def choose_profit_action(position: dict[str, Any], quotes: dict[str, Any], config: dict[str, Any]) -> str:
    symbol = position.get("symbol")
    quote = quotes.get(symbol, {})
    spy = quotes.get(config["strategy"]["benchmark_symbol"], {})
    vix = quotes.get("VIX", {})
    if vix and float(vix.get("price", 0.0)) >= float(config["strategy"]["vix_caution_level"]):
        return "full_sell"
    if spy and float(spy.get("trend_score", 1.0)) < 0:
        return "full_sell"
    if quote and float(quote.get("trend_score", 1.0)) > 0.7:
        return "trail_only"
    return "partial_sell"


def execute_profit_target(position: dict[str, Any], action: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    quantity = int(position["quantity"])
    if action == "trail_only":
        return [{"type": "raise_or_maintain_trailing_stop", "symbol": position["symbol"], "quantity": quantity}]
    if action == "full_sell":
        sell_quantity = quantity
    else:
        sell_quantity = max(1, int(quantity * float(config["risk"]["profit_target_partial_sell_pct"]) / 100.0))
    return [
        {"type": "cancel_or_reduce_protective_stop", "symbol": position["symbol"], "quantity": sell_quantity},
        {
            "type": "place_profit_limit_sell",
            "symbol": position["symbol"],
            "quantity": sell_quantity,
            "limit_price": round(float(position["target_price"]), 2),
        },
        {"type": "replace_stop_for_remaining_quantity", "symbol": position["symbol"]},
    ]


def handle_exit_fill(state: dict[str, Any], fill: dict[str, Any]) -> list[dict[str, Any]]:
    symbol = fill["symbol"]
    positions = tracked_positions(state)
    position = next((item for item in positions if item.get("symbol") == symbol), None)
    if not position:
        return []
    remaining = int(position["quantity"]) - int(fill["quantity"])
    if remaining <= 0:
        state["positions"] = [item for item in positions if item.get("symbol") != symbol]
        state["position"] = state["positions"][0] if state["positions"] else None
        return [event("position_closed", "Position fully closed.", symbol=fill["symbol"])]
    position["quantity"] = remaining
    return [event("position_reduced", "Position reduced; stop must match remaining quantity.", symbol=fill["symbol"], remaining=remaining)]


def monitor_active_position(
    state: dict[str, Any],
    quotes: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events = detect_price_events(state, quotes, config)
    actions: list[dict[str, Any]] = []
    for target_event in [item for item in events if item["kind"] == "target_reached"]:
        symbol = target_event["details"]["symbol"]
        position = next((item for item in tracked_positions(state) if item.get("symbol") == symbol), None)
        if not position:
            continue
        profit_action = choose_profit_action(position, quotes, config)
        events.append(event("profit_action_chosen", "Dynamic profit action selected.", symbol=symbol, action=profit_action))
        actions.extend(execute_profit_target(position, profit_action, config))
    return events, actions


def next_poll_interval(state: dict[str, Any], events: list[dict[str, Any]], config: dict[str, Any]) -> int | None:
    if state.get("open_orders") or any(item["kind"] in {"near_stop", "near_target", "target_reached"} for item in events):
        return int(config["monitoring"]["elevated_poll_seconds"])
    if tracked_positions(state):
        return int(config["monitoring"]["open_position_poll_seconds"])
    if state.get("pending_candidates"):
        return None
    return None


def apply_strategy_refinements(state: dict[str, Any], market: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    overrides = state.setdefault("strategy_overrides", {})
    vix = float(market.get("VIX", {}).get("price", 0.0)) if market.get("VIX") else 0.0
    old_gap = float(overrides.get("next_session_max_gap_pct", config["strategy"]["next_session_max_gap_pct"]))
    if vix >= float(config["strategy"]["vix_caution_level"]):
        new_gap = max(0.25, old_gap - 0.1)
    else:
        new_gap = min(1.0, old_gap + 0.05)
    overrides["next_session_max_gap_pct"] = round(new_gap, 2)
    if new_gap != old_gap:
        return [event("strategy_refined", "Soft strategy parameter adjusted.", parameter="next_session_max_gap_pct", value=round(new_gap, 2))]
    return []


def daily_reflection(state: dict[str, Any], account: dict[str, Any], quotes: dict[str, Any], config: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    events = apply_strategy_refinements(state, quotes, config)
    brief = build_daily_brief(state, account, quotes, events)
    return brief, events


def build_trade_audit_note(event_item: dict[str, Any], actions: list[dict[str, Any]]) -> str:
    return f"{event_item['kind']}: {event_item['message']} | actions={len(actions)}"


def build_daily_brief(
    state: dict[str, Any],
    account: dict[str, Any],
    quotes: dict[str, Any],
    refinement_events: list[dict[str, Any]],
) -> str:
    positions = tracked_positions(state)
    position_label = ", ".join(position.get("symbol", "unknown") for position in positions) if positions else "flat"
    stop_label = ", ".join(
        f"{position.get('symbol')} {round(float(position['stop_price']), 2)}"
        for position in positions
        if position.get("stop_price")
    ) or "n/a"
    target_label = ", ".join(
        f"{position.get('symbol')} {round(float(position['target_price']), 2)}"
        for position in positions
        if position.get("target_price")
    ) or "n/a"
    lines = [
        "| Item | Value |",
        "|---|---:|",
        f"| Equity | {account.get('equity', account.get('account_value', 'n/a'))} |",
        f"| Buying power | {account.get('buying_power', 'n/a')} |",
        f"| Status | {state.get('status')} |",
        f"| Daily buys | {state.get('daily_buy_count', 0)} |",
        f"| Position | {position_label} |",
        f"| Stop | {stop_label} |",
        f"| Target | {target_label} |",
        f"| Refinements | {len(refinement_events)} |",
    ]
    return "\n".join(lines)


def run_monitor(
    state: dict[str, Any],
    config: dict[str, Any],
    account: dict[str, Any],
    orders: list[dict[str, Any]],
    quotes: dict[str, Any],
    candidates: list[dict[str, Any]],
    mode: str,
    trading_date: str,
) -> MonitorResult:
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    state["pending_candidates"] = candidates
    events.extend(reset_daily_counters(state, account, trading_date))
    events.extend(reconcile_account(state, account, orders))
    events.extend(detect_manual_activity(state, account, orders))
    events.extend(daily_loss_guard(state, account, config))

    watched_symbols = []
    for position in tracked_positions(state):
        if position.get("symbol"):
            watched_symbols.append(position["symbol"])
    watched_symbols.extend(candidate["symbol"] for candidate in candidates)
    watched_symbols.extend([config["strategy"]["benchmark_symbol"], "VIX"])
    live_quotes = fetch_live_quotes(sorted(set(watched_symbols)), quotes)

    if mode == "pending":
        new_events, new_actions = monitor_pending_candidates(state, account, live_quotes, config)
        events.extend(new_events)
        actions.extend(new_actions)
    elif mode == "position":
        new_events, new_actions = monitor_active_position(state, live_quotes, config)
        events.extend(new_events)
        actions.extend(new_actions)
    elif mode == "daily":
        brief, refinement_events = daily_reflection(state, account, live_quotes, config)
        events.extend(refinement_events)
        state["last_events"] = events[-50:]
        return MonitorResult(state, events, actions, next_poll_interval(state, events, config), brief)

    state["last_events"] = events[-50:]
    return MonitorResult(state, events, actions, next_poll_interval(state, events, config))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Agentic event-driven monitor on supplied snapshots.")
    parser.add_argument("--config", default="strategy_config.json")
    parser.add_argument("--state", default="agentic_state.json")
    parser.add_argument("--account-json", required=True, help="Account snapshot JSON or path.")
    parser.add_argument("--orders-json", default="[]", help="Orders snapshot JSON or path.")
    parser.add_argument("--quotes-json", default="{}", help="Quotes snapshot JSON or path.")
    parser.add_argument("--candidates-json", default="[]", help="Candidate list JSON or path.")
    parser.add_argument("--mode", choices=["pending", "position", "daily"], default="daily")
    parser.add_argument("--trading-date", default=datetime.now().date().isoformat())
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists() and args.config == "strategy_config.json":
        config_path = Path(__file__).with_name("strategy_config.json")
    config = load_json(str(config_path), {})

    state_path = Path(args.state)
    state = load_state(state_path)
    account = load_json(args.account_json, {})
    orders = load_json(args.orders_json, [])
    quotes = load_json(args.quotes_json, {})
    candidates = load_json(args.candidates_json, [])

    result = run_monitor(state, config, account, orders, quotes, candidates, args.mode, args.trading_date)
    if not args.no_save:
        save_state(state_path, result.state)

    print(
        json.dumps(
            {
                "events": result.events,
                "actions": result.actions,
                "next_poll_seconds": result.next_poll_seconds,
                "daily_brief": result.daily_brief,
                "state": result.state,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
