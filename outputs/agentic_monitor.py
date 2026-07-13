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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import agentic_task_queue


DEFAULT_STATE: dict[str, Any] = {
    "trading_date": None,
    "status": "ready",
    "paused_reason": None,
    "daily_buy_count": 0,
    "start_of_day_equity": None,
    "last_account_snapshot": None,
    "position": None,
    "positions": [],
    "option_positions": [],
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


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
    option_positions = account.get("option_positions", [])
    state["account_positions"] = positions
    state["account_option_positions"] = option_positions
    sync_tracked_positions(state, positions)
    sync_tracked_option_positions(state, option_positions)

    if prior and prior.get("positions") != account.get("positions"):
        events.append(event("account_position_change", "Position state changed.", positions=account.get("positions", [])))
    if prior and prior.get("option_positions") != account.get("option_positions"):
        events.append(
            event("option_position_change", "Option position state changed.", option_positions=account.get("option_positions", []))
        )
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
    state["active_positions"] = deepcopy(synced)
    state["active_position"] = deepcopy(synced[0]) if synced else None


def sync_authorization_scope(state: dict[str, Any], config: dict[str, Any]) -> None:
    scope = state.setdefault("authorization_scope", {})
    risk_config = config.get("risk", {})
    scope["max_planned_risk_pct"] = risk_config.get("risk_per_trade_pct")
    scope["max_position_pct"] = risk_config.get("max_position_pct")
    scope["total_open_risk_pct"] = risk_config.get("total_open_risk_pct")
    scope["max_positions_standard"] = risk_config.get("max_positions_standard")
    scope["min_cash_reserve_pct"] = risk_config.get("min_cash_reserve_pct")


def tracked_positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    positions = state.get("positions")
    if isinstance(positions, list) and positions:
        return [position for position in positions if position]
    position = state.get("position")
    return [position] if position else []


def sync_tracked_option_positions(state: dict[str, Any], account_positions: list[dict[str, Any]]) -> None:
    by_id = {position.get("option_id"): position for position in account_positions if position.get("option_id")}
    tracked = tracked_option_positions(state)
    synced: list[dict[str, Any]] = []

    for position in tracked:
        option_id = position.get("option_id")
        account_position = by_id.get(option_id)
        if not account_position:
            continue
        merged = deepcopy(position)
        merged.update({key: value for key, value in account_position.items() if value is not None})
        synced.append(merged)

    tracked_ids = {position.get("option_id") for position in synced}
    for option_id, account_position in by_id.items():
        if option_id not in tracked_ids:
            synced.append(account_position)

    state["option_positions"] = synced


def tracked_option_positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    positions = state.get("option_positions")
    if isinstance(positions, list):
        return [position for position in positions if position and float(position.get("quantity", 0) or 0) > 0]
    return []


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
        position["highest_price"] = max(float(position.get("highest_price", price) or price), price)
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


def allow_add_to_existing_positions(config: dict[str, Any]) -> bool:
    return bool(config["strategy"].get("allow_add_to_existing_positions", False))


def option_trading_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("execution", {}).get("allow_options", False)) and bool(
        config.get("options_strategy", {}).get("enabled", False)
    )


def account_option_approved(account: dict[str, Any], config: dict[str, Any]) -> bool:
    required = set(config.get("options_strategy", {}).get("require_account_option_level", []))
    return str(account.get("option_level", "")) in required


def is_option_candidate(candidate: dict[str, Any]) -> bool:
    return str(candidate.get("asset_type", "")).lower() == "option"


def option_quote_price(quote: dict[str, Any]) -> float | None:
    for key in ("price", "mark_price", "last_trade_price", "last_price", "ask_price", "bid_price"):
        value = quote.get(key)
        if value not in (None, ""):
            return float(value)
    bid = quote.get("bid")
    ask = quote.get("ask")
    if bid not in (None, "") and ask not in (None, ""):
        return (float(bid) + float(ask)) / 2.0
    return None


def option_entry_price(position: dict[str, Any]) -> float:
    for key in ("entry_price", "average_price", "average_buy_price", "avg_price"):
        value = position.get(key)
        if value not in (None, ""):
            return float(value)
    return 0.0


def option_dte(position: dict[str, Any], trading_date: str | None = None) -> int | None:
    expiration = position.get("expiration_date")
    if not expiration:
        return None
    today = date.fromisoformat(trading_date) if trading_date else datetime.now(timezone.utc).date()
    return (date.fromisoformat(str(expiration)) - today).days


def option_close_order_for_position(state: dict[str, Any], option_id: str) -> dict[str, Any] | None:
    for order in state.get("open_orders", []):
        if (
            order.get("asset_type") == "option"
            and order.get("option_id") == option_id
            and order.get("side") == "sell"
            and order.get("position_effect") == "close"
            and order.get("state") in {"open", "queued", "confirmed"}
        ):
            return order
    return None


def position_risk_dollars(position: dict[str, Any]) -> float:
    quantity = float(position.get("quantity", 0.0))
    entry = float(position.get("entry_price", position.get("average_buy_price", 0.0)) or 0.0)
    stop = float(position.get("stop_price", 0.0) or 0.0)
    if quantity <= 0 or entry <= 0 or stop <= 0:
        return 0.0
    return max(0.0, (entry - stop) * quantity)


def position_age_minutes(position: dict[str, Any]) -> float | None:
    opened_at = parse_iso(position.get("opened_at"))
    if not opened_at:
        return None
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0


def protective_stop_order_for_symbol(state: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    stops = protective_stop_orders_for_symbol(state, symbol)
    if not stops:
        return None
    return max(stops, key=lambda order: float(order.get("stop_price", 0) or 0))


def protective_stop_orders_for_symbol(state: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    stops: list[dict[str, Any]] = []
    for order in state.get("open_orders", []):
        if (
            order.get("symbol") == symbol
            and order.get("side") == "sell"
            and order.get("trigger") == "stop"
            and order.get("state") in {"open", "queued", "confirmed"}
        ):
            stops.append(order)
    return stops


def account_position_by_symbol(account: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    for position in account.get("positions", []) or []:
        if position.get("symbol") == symbol:
            return position
    return None


def account_position_value(account: dict[str, Any], symbol: str, fallback_price: float) -> float:
    position = account_position_by_symbol(account, symbol)
    if not position:
        return 0.0
    quantity = float(position.get("quantity", 0) or 0)
    return quantity * fallback_price


def reconcile_protective_stops(
    state: dict[str, Any],
    account: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if not bool(config["execution"].get("auto_place_protective_stop_after_buy_fill", True)):
        return events, actions

    had_repair = False
    for position in account.get("positions", []) or []:
        symbol = position.get("symbol")
        quantity = int(float(position.get("quantity", 0) or 0))
        if not symbol or quantity <= 0:
            continue
        stops = protective_stop_orders_for_symbol(state, symbol)
        matching_stops = [order for order in stops if int(float(order.get("quantity", 0) or 0)) == quantity]
        tracked = next((item for item in tracked_positions(state) if item.get("symbol") == symbol), {})
        if len(stops) == 1 and len(matching_stops) == 1:
            stop = stops[0]
            tracked["protective_stop_order_id"] = stop.get("id")
            tracked["stop_price"] = float(stop.get("stop_price", tracked.get("stop_price", 0.0)) or 0.0)
            tracked["stop_quantity"] = quantity
            continue

        had_repair = True
        existing_stop_prices = [float(order.get("stop_price", 0) or 0) for order in stops if order.get("stop_price")]
        tracked_stop = float(tracked.get("stop_price", 0.0) or position.get("stop_price", 0.0) or 0.0)
        stop_price = max(existing_stop_prices + ([tracked_stop] if tracked_stop > 0 else []), default=0.0)
        state["status"] = "paused"
        state["paused_reason"] = "protective_stop_reconciliation_required"
        repair_reason = "missing_stop" if not stops else "split_or_wrong_quantity_stop"
        if stop_price <= 0:
            actions.append(
                {
                    "type": "protective_stop_repair_blocked",
                    "reason": "missing_stop_price",
                    "symbol": symbol,
                    "quantity": quantity,
                    "quantity_scope": "full_position",
                    "cancel_existing_symbol_stops_first": bool(stops),
                    "cancel_existing_order_ids": [order.get("id") for order in stops if order.get("id")],
                }
            )
            events.append(
                event(
                    "protective_stop_repair_blocked",
                    "Protective stop repair is blocked until a valid stop price is available.",
                    symbol=symbol,
                    quantity=quantity,
                    open_stop_count=len(stops),
                    reason="missing_stop_price",
                )
            )
            continue
        actions.append(
            {
                "type": "place_protective_stop",
                "reason": repair_reason,
                "symbol": symbol,
                "side": "sell",
                "quantity": quantity,
                "quantity_scope": "full_position",
                "consolidate_by_symbol": True,
                "cancel_existing_symbol_stops_first": True,
                "cancel_existing_order_ids": [order.get("id") for order in stops if order.get("id")],
                "order_type": config["execution"]["protective_stop_order_type"],
                "stop_price": round(stop_price, 2),
                "time_in_force": config["execution"]["protective_stop_time_in_force"],
            }
        )
        events.append(
            event(
                "protective_stop_repair_required",
                "Protective stop must be repaired before new buys.",
                symbol=symbol,
                quantity=quantity,
                open_stop_count=len(stops),
                stop_price=round(stop_price, 2),
                reason=repair_reason,
            )
        )

    if not had_repair and state.get("paused_reason") == "protective_stop_reconciliation_required":
        state["status"] = "active"
        state["paused_reason"] = None
        events.append(event("protective_stop_reconciliation_clear", "Protective stops verified; pause cleared."))
    return events, actions


def proposed_ratchet_stop(position: dict[str, Any], price: float, config: dict[str, Any]) -> tuple[float | None, str | None]:
    risk_config = config["risk"]
    if not bool(risk_config.get("protective_stop_ratchet_enabled", False)):
        return None, None

    age = position_age_minutes(position)
    min_age = float(risk_config.get("min_minutes_before_stop_ratchet", 0.0))
    if age is not None and age < min_age:
        return None, None

    entry = float(position.get("entry_price", position.get("average_buy_price", 0.0)) or 0.0)
    current_stop = float(position.get("stop_price", 0.0) or 0.0)
    initial_stop = float(position.get("initial_stop_price", current_stop) or current_stop)
    if entry <= 0 or current_stop <= 0 or initial_stop <= 0 or initial_stop >= entry or price <= entry:
        return None, None

    initial_risk = entry - initial_stop
    current_r = (price - entry) / initial_risk
    highest_price = max(float(position.get("highest_price", price) or price), price)
    proposed = current_stop
    reason = None

    if current_r >= float(risk_config.get("breakeven_stop_trigger_r_multiple", 0.75)):
        proposed = max(proposed, entry)
        reason = "breakeven"

    if current_r >= float(risk_config.get("trail_stop_trigger_r_multiple", 1.0)):
        trailing_stop = highest_price * (1.0 - float(risk_config["trailing_stop_pct"]) / 100.0)
        if trailing_stop > proposed:
            proposed = trailing_stop
            reason = "trailing_high_watermark"

    if proposed <= current_stop:
        return None, None

    min_below_price = float(risk_config.get("min_stop_below_current_price_pct", 1.0))
    max_safe_stop = price * (1.0 - min_below_price / 100.0)
    proposed = min(proposed, max_safe_stop)
    min_raise_pct = float(risk_config.get("min_stop_raise_pct", 0.5))
    if proposed <= current_stop * (1.0 + min_raise_pct / 100.0):
        return None, None

    return round(proposed, 2), reason


def ratchet_protective_stops(
    state: dict[str, Any],
    quotes: dict[str, Any],
    config: dict[str, Any],
    skip_symbols: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    skip_symbols = skip_symbols or set()
    for position in tracked_positions(state):
        symbol = position.get("symbol")
        if symbol in skip_symbols:
            continue
        quote = quotes.get(symbol)
        if not symbol or not quote:
            continue
        price = float(quote["price"])
        new_stop, reason = proposed_ratchet_stop(position, price, config)
        if new_stop is None:
            continue
        stop_order = protective_stop_order_for_symbol(state, symbol)
        quantity = int(float(position.get("quantity", 0)))
        if quantity <= 0:
            continue
        action = {
            "type": "raise_or_replace_protective_stop",
            "symbol": symbol,
            "quantity": quantity,
            "current_stop_price": round(float(position["stop_price"]), 2),
            "new_stop_price": new_stop,
            "reason": reason,
            "order_type": config["execution"]["protective_stop_order_type"],
            "time_in_force": config["execution"]["protective_stop_time_in_force"],
            "protective_stop_order_id": stop_order.get("id") if stop_order else position.get("protective_stop_order_id"),
            "requires_cancel_replace": True,
        }
        actions.append(action)
        events.append(
            event(
                "protective_stop_ratchet_planned",
                "Protective stop ratchet planned.",
                symbol=symbol,
                price=price,
                current_stop=round(float(position["stop_price"]), 2),
                new_stop=new_stop,
                reason=reason,
            )
        )
    return events, actions


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
    if int(state.get("daily_buy_count", 0)) >= int(config["execution"]["max_auto_buys_per_day"]):
        return events, actions

    held_symbols = open_position_symbols(state, account)
    add_to_existing_allowed = allow_add_to_existing_positions(config)
    max_positions_reached = current_open_position_count(state, account) >= max_open_positions(account, config)
    group_counts = open_group_counts(state, account, config)
    max_group_count = max_positions_per_group(account, config)
    remaining_risk = remaining_open_risk_dollars(state, account, config)
    equity = float(account.get("equity", account.get("account_value", 0.0)) or 0.0)
    buying_power = float(account.get("buying_power", account.get("cash", 0.0)) or 0.0)
    min_cash_reserve = equity * (float(config["risk"].get("min_cash_reserve_pct", 0.0)) / 100.0)
    max_position_value = equity * (float(config["risk"]["max_position_pct"]) / 100.0)
    for candidate in state.get("pending_candidates", []):
        symbol = candidate["symbol"]
        is_held_symbol = symbol in held_symbols
        if max_positions_reached and not (add_to_existing_allowed and is_held_symbol):
            events.append(event("max_positions_reached", "Candidate buying skipped because max open positions is reached.", symbol=symbol))
            continue
        if is_held_symbol and not add_to_existing_allowed:
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
        candidate_cost = int(candidate.get("shares", 0)) * live_price
        if buying_power - candidate_cost < min_cash_reserve:
            events.append(
                event(
                    "candidate_cash_reserve_skip",
                    "Candidate skipped because it would breach the cash reserve.",
                    symbol=symbol,
                    estimated_cost=round(candidate_cost, 2),
                    required_cash_reserve=round(min_cash_reserve, 2),
                )
            )
            continue
        if candidate_cost > max_position_value:
            events.append(
                event(
                    "candidate_position_exposure_skip",
                    "Candidate skipped because order value exceeds max position exposure.",
                    symbol=symbol,
                    estimated_cost=round(candidate_cost, 2),
                    max_position_value=round(max_position_value, 2),
                )
            )
            continue
        if is_held_symbol:
            aggregate_value = account_position_value(account, symbol, live_price) + candidate_cost
            if aggregate_value > max_position_value:
                events.append(
                    event(
                        "candidate_add_on_exposure_skip",
                        "Held-symbol add-on skipped because aggregate symbol exposure would exceed cap.",
                        symbol=symbol,
                        aggregate_value=round(aggregate_value, 2),
                        max_position_value=round(max_position_value, 2),
                    )
                )
                continue
        if is_option_candidate(candidate):
            if not option_trading_enabled(config):
                events.append(event("option_trading_disabled", "Option candidate skipped because option trading is disabled.", symbol=symbol))
                continue
            if not account_option_approved(account, config):
                events.append(
                    event(
                        "option_trading_not_approved",
                        "Option candidate skipped because the Agentic account lacks required option approval.",
                        symbol=symbol,
                        option_level=account.get("option_level", ""),
                    )
                )
                continue
            if not candidate.get("option_id") or not candidate.get("option_limit_price"):
                events.append(
                    event(
                        "option_contract_missing",
                        "Option candidate skipped because no resolved option contract was supplied.",
                        symbol=symbol,
                    )
                )
                continue
            action = review_and_place_option_buy_to_open(state, account, candidate, live_price, config)
            actions.append(action)
            events.append(event("auto_option_buy_planned", "Automatic option buy action prepared.", symbol=symbol, live_price=live_price))
            break
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


def review_and_place_option_buy_to_open(
    state: dict[str, Any],
    account: dict[str, Any],
    candidate: dict[str, Any],
    live_price: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    options = config["options_strategy"]
    option_id = str(candidate.get("option_id", ""))
    if not option_id:
        raise ValueError("Option candidate missing option_id")
    quantity = int(candidate.get("contracts", candidate.get("quantity", 1)))
    limit_price = float(candidate["option_limit_price"])
    estimated_premium = quantity * limit_price * 100.0
    return {
        "type": "review_and_place_option_buy_to_open",
        "asset_type": "option",
        "symbol": candidate["symbol"],
        "chain_symbol": candidate.get("chain_symbol", candidate["symbol"]),
        "underlying_type": candidate.get("underlying_type", "equity"),
        "option_id": option_id,
        "option_type": candidate.get("option_type", "call"),
        "strategy": candidate.get("option_strategy", "long_call"),
        "quantity": str(quantity),
        "legs": [
            {
                "option_id": option_id,
                "side": "buy",
                "position_effect": "open",
                "ratio_quantity": 1,
            }
        ],
        "order_type": options.get("order_type", "limit"),
        "price": f"{limit_price:.2f}",
        "time_in_force": options.get("time_in_force", "gfd"),
        "market_hours": options.get("market_hours", "regular_hours"),
        "estimated_cost": round(estimated_premium, 2),
        "estimated_max_loss": round(float(candidate.get("max_loss", estimated_premium)), 2),
        "underlying_live_price": live_price,
        "requires_broker_review": bool(config["execution"]["require_broker_review_before_order"]),
        "approval_gate": {
            "agentic_allowed": bool(account.get("agentic_allowed", True)),
            "option_level": account.get("option_level", ""),
            "required_option_levels": options.get("require_account_option_level", []),
        },
        "exit_plan": {
            "sell_to_close": True,
            "synthetic_monitoring": True,
            "no_broker_protective_stop": True,
        },
    }


def review_and_place_option_sell_to_close(
    position: dict[str, Any],
    limit_price: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    options = config["options_strategy"]
    option_id = str(position["option_id"])
    quantity = int(position.get("quantity", position.get("contracts", 1)))
    return {
        "type": "review_and_place_option_sell_to_close",
        "asset_type": "option",
        "symbol": position["symbol"],
        "chain_symbol": position.get("chain_symbol", position["symbol"]),
        "underlying_type": position.get("underlying_type", "equity"),
        "option_id": option_id,
        "option_type": position.get("option_type", "call"),
        "quantity": str(quantity),
        "legs": [
            {
                "option_id": option_id,
                "side": "sell",
                "position_effect": "close",
                "ratio_quantity": 1,
            }
        ],
        "order_type": options.get("order_type", "limit"),
        "price": f"{float(limit_price):.2f}",
        "time_in_force": options.get("time_in_force", "gfd"),
        "market_hours": options.get("market_hours", "regular_hours"),
        "requires_broker_review": bool(config["execution"]["require_broker_review_before_order"]),
    }


def cancel_option_order_action(order_id: str, symbol: str | None = None) -> dict[str, Any]:
    return {
        "type": "cancel_option_order",
        "asset_type": "option",
        "order_id": order_id,
        "symbol": symbol,
    }


def option_exit_reason(
    position: dict[str, Any],
    option_price: float,
    underlying_quote: dict[str, Any] | None,
    config: dict[str, Any],
    trading_date: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    options_exit = config.get("options_exit", {})
    entry = option_entry_price(position)
    if entry <= 0:
        return "missing_entry_price", {"option_price": option_price}

    gain_pct = ((option_price - entry) / entry) * 100.0
    if gain_pct >= float(options_exit.get("profit_target_pct", 50.0)):
        return "profit_target", {"gain_pct": round(gain_pct, 2), "entry_price": entry, "option_price": option_price}

    loss_pct = ((entry - option_price) / entry) * 100.0
    if loss_pct >= float(options_exit.get("stop_loss_pct", 35.0)):
        return "stop_loss", {"loss_pct": round(loss_pct, 2), "entry_price": entry, "option_price": option_price}

    dte = option_dte(position, trading_date)
    if dte is not None and dte <= int(options_exit.get("min_dte_exit", 14)):
        return "time_stop", {"dte": dte}

    if bool(options_exit.get("use_underlying_stop", True)) and underlying_quote:
        underlying_stop = position.get("underlying_stop_price")
        if underlying_stop not in (None, "") and float(underlying_quote["price"]) <= float(underlying_stop):
            return (
                "underlying_stop",
                {"underlying_price": float(underlying_quote["price"]), "underlying_stop_price": float(underlying_stop)},
            )

    return None, {"gain_pct": round(gain_pct, 2)}


def monitor_option_positions(
    state: dict[str, Any],
    account: dict[str, Any],
    quotes: dict[str, Any],
    config: dict[str, Any],
    trading_date: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if not option_trading_enabled(config) or not config.get("options_exit", {}).get("enabled", False):
        return events, actions

    if not account_option_approved(account, config):
        if tracked_option_positions(state):
            events.append(
                event(
                    "option_exit_not_approved",
                    "Option exits blocked because the Agentic account lacks required option approval.",
                    option_level=account.get("option_level", ""),
                )
            )
        return events, actions

    for position in tracked_option_positions(state):
        option_id = str(position.get("option_id", ""))
        if not option_id:
            events.append(event("option_position_missing_id", "Option position skipped because option_id is missing."))
            continue
        if config.get("options_exit", {}).get("avoid_duplicate_close_orders", True) and option_close_order_for_position(
            state, option_id
        ):
            events.append(
                event("option_close_order_exists", "Option exit skipped because a close order is already open.", option_id=option_id)
            )
            continue
        quote = quotes.get(option_id)
        if not quote:
            events.append(
                event(
                    "option_quote_missing",
                    "Option exit skipped because no live option quote was available.",
                    symbol=position.get("symbol"),
                    option_id=option_id,
                )
            )
            continue
        option_price = option_quote_price(quote)
        if option_price is None:
            events.append(
                event(
                    "option_quote_invalid",
                    "Option exit skipped because live option quote had no usable price.",
                    symbol=position.get("symbol"),
                    option_id=option_id,
                )
            )
            continue
        underlying_symbol = str(position.get("symbol") or position.get("chain_symbol") or "")
        reason, details = option_exit_reason(
            position,
            option_price,
            quotes.get(underlying_symbol),
            config,
            trading_date,
        )
        if not reason:
            continue
        if reason == "missing_entry_price":
            events.append(
                event(
                    "option_entry_missing",
                    "Option exit skipped because entry price is missing.",
                    symbol=position.get("symbol"),
                    option_id=option_id,
                )
            )
            continue
        action = review_and_place_option_sell_to_close(position, option_price, config)
        action["exit_reason"] = reason
        actions.append(action)
        events.append(
            event(
                "option_exit_planned",
                "Automatic option sell-to-close action prepared.",
                symbol=position.get("symbol"),
                option_id=option_id,
                reason=reason,
                **details,
            )
        )
    return events, actions


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
        "initial_stop_price": stop,
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
    consolidate = bool(config["execution"].get("consolidate_protective_stops_by_symbol", True))
    return {
        "type": "place_protective_stop",
        "symbol": symbol,
        "side": "sell",
        "quantity": quantity,
        "quantity_scope": "full_position" if consolidate else "fill_quantity",
        "consolidate_by_symbol": consolidate,
        "cancel_existing_symbol_stops_first": consolidate,
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
    account: dict[str, Any],
    quotes: dict[str, Any],
    config: dict[str, Any],
    trading_date: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events = detect_price_events(state, quotes, config)
    actions: list[dict[str, Any]] = []
    option_events, option_actions = monitor_option_positions(state, account, quotes, config, trading_date)
    events.extend(option_events)
    actions.extend(option_actions)
    target_symbols = {item["details"]["symbol"] for item in events if item["kind"] == "target_reached"}
    ratchet_events, ratchet_actions = ratchet_protective_stops(state, quotes, config, target_symbols)
    events.extend(ratchet_events)
    actions.extend(ratchet_actions)
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


def ledger_records(events: list[dict[str, Any]], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for action in actions:
        action_type = action.get("type")
        if action_type == "review_and_place_equity_buy":
            records.append(
                {
                    "time": now_iso(),
                    "record_type": "candidate_planned",
                    "symbol": action.get("symbol"),
                    "setup_type": action.get("setup_type"),
                    "entry": action.get("limit_price"),
                    "shares": action.get("quantity"),
                    "estimated_cost": action.get("estimated_cost"),
                    "estimated_max_loss": action.get("estimated_max_loss"),
                    "reward_risk_ratio": action.get("reward_risk_ratio"),
                    "sector_group": action.get("sector_group"),
                    "target": (action.get("after_fill") or {}).get("target_price"),
                    "partial_target": (action.get("after_fill") or {}).get("partial_target"),
                }
            )
        elif action_type == "place_protective_stop":
            records.append(
                {
                    "time": now_iso(),
                    "record_type": "protective_stop_action",
                    "symbol": action.get("symbol"),
                    "shares": action.get("quantity"),
                    "stop": action.get("stop_price"),
                    "reason": action.get("reason"),
                }
            )
    for item in events:
        kind = item.get("kind")
        details = item.get("details", {})
        if kind == "buy_filled":
            records.append(
                {
                    "time": item.get("time"),
                    "record_type": "buy_fill",
                    "symbol": details.get("symbol"),
                    "shares": details.get("quantity"),
                    "entry": details.get("entry"),
                }
            )
        elif kind in {"position_closed", "position_reduced"}:
            records.append(
                {
                    "time": item.get("time"),
                    "record_type": "exit_event",
                    "symbol": details.get("symbol"),
                    "remaining": details.get("remaining"),
                    "exit_reason": kind,
                }
            )
        elif kind.startswith("candidate_") and kind.endswith("_skip"):
            records.append(
                {
                    "time": item.get("time"),
                    "record_type": "candidate_rejected",
                    "symbol": details.get("symbol"),
                    "rejection_reason": kind,
                }
            )
    return [record for record in records if record.get("symbol")]


def append_trade_ledger(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


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

    sync_authorization_scope(state, config)
    state["pending_candidates"] = candidates
    events.extend(reset_daily_counters(state, account, trading_date))
    events.extend(reconcile_account(state, account, orders))
    events.extend(detect_manual_activity(state, account, orders))
    events.extend(daily_loss_guard(state, account, config))
    repair_events, repair_actions = reconcile_protective_stops(state, account, config)
    events.extend(repair_events)
    actions.extend(repair_actions)

    watched_symbols = []
    for position in tracked_positions(state):
        if position.get("symbol"):
            watched_symbols.append(position["symbol"])
    for position in tracked_option_positions(state):
        if position.get("option_id"):
            watched_symbols.append(position["option_id"])
        if position.get("symbol"):
            watched_symbols.append(position["symbol"])
    watched_symbols.extend(candidate["symbol"] for candidate in candidates)
    watched_symbols.extend([config["strategy"]["benchmark_symbol"], "VIX"])
    live_quotes = fetch_live_quotes(sorted(set(watched_symbols)), quotes)

    if repair_actions:
        pass
    elif mode == "pending":
        new_events, new_actions = monitor_pending_candidates(state, account, live_quotes, config)
        events.extend(new_events)
        actions.extend(new_actions)
    elif mode == "position":
        new_events, new_actions = monitor_active_position(state, account, live_quotes, config, trading_date)
        events.extend(new_events)
        actions.extend(new_actions)
    elif mode == "daily":
        brief, refinement_events = daily_reflection(state, account, live_quotes, config)
        events.extend(refinement_events)
        _, task_events = agentic_task_queue.reconcile_tasks(state, account, live_quotes, config, trading_date)
        for task_event in task_events:
            events.append(
                event(
                    task_event["kind"],
                    "Agentic task queue flagged unresolved broker monitoring risk.",
                    symbol=task_event.get("symbol"),
                    reason=task_event.get("reason"),
                )
            )
        state["last_events"] = events[-50:]
        return MonitorResult(state, events, actions, next_poll_interval(state, events, config), brief)

    _, task_events = agentic_task_queue.reconcile_tasks(state, account, live_quotes, config, trading_date)
    for task_event in task_events:
        events.append(
            event(
                task_event["kind"],
                "Agentic task queue flagged unresolved broker monitoring risk.",
                symbol=task_event.get("symbol"),
                reason=task_event.get("reason"),
            )
        )

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
    parser.add_argument("--tasks-json", default="work/agentic_tasks.json")
    parser.add_argument("--tasks-md", default="work/agentic_tasks.md")
    parser.add_argument("--ledger-jsonl", default="work/agentic_trade_ledger.jsonl")
    parser.add_argument("--no-ledger", action="store_true")
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
        agentic_task_queue.save_task_files(Path(args.tasks_json), Path(args.tasks_md), result.state.get("agentic_tasks", []))
        if not args.no_ledger:
            append_trade_ledger(Path(args.ledger_jsonl), ledger_records(result.events, result.actions))

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
