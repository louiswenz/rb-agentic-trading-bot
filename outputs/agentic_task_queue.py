#!/usr/bin/env python3
"""Persistent task queue helpers for Agentic broker monitoring.

The queue is derived from broker snapshots and local strategy state. It is not
an execution engine; it makes unresolved operational work explicit so the
orchestrator can decide whether a heartbeat should stay quiet, notify, or act.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OPEN_ORDER_STATES = {"open", "queued", "confirmed", "new", "unconfirmed", "partially_filled"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def order_quantity(order: dict[str, Any]) -> int:
    return int(float(order.get("quantity", 0) or 0))


def position_quantity(position: dict[str, Any]) -> int:
    return int(float(position.get("quantity", 0) or 0))


def open_orders(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [order for order in state.get("open_orders", []) if order.get("state") in OPEN_ORDER_STATES]


def stop_orders_for_symbol(state: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    return [
        order
        for order in open_orders(state)
        if order.get("symbol") == symbol and order.get("side") == "sell" and order.get("trigger") == "stop"
    ]


def queued_buy_orders(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        order
        for order in open_orders(state)
        if order.get("side") == "buy" and order.get("type") == "limit" and order.get("symbol")
    ]


def tracked_positions(state: dict[str, Any], account: dict[str, Any]) -> list[dict[str, Any]]:
    positions = account.get("positions") or state.get("positions") or []
    return [position for position in positions if position and position.get("symbol")]


def tracked_option_positions(state: dict[str, Any], account: dict[str, Any]) -> list[dict[str, Any]]:
    positions = account.get("option_positions") or state.get("option_positions") or []
    return [position for position in positions if position and float(position.get("quantity", 0) or 0) > 0]


def task_id(task_type: str, symbol: str, suffix: str = "") -> str:
    base = f"{task_type}:{symbol.upper()}"
    return f"{base}:{suffix}" if suffix else base


def build_task(
    task_type: str,
    symbol: str,
    status: str,
    priority: str,
    summary: str,
    next_check: str,
    details: dict[str, Any] | None = None,
    suffix: str = "",
) -> dict[str, Any]:
    return {
        "id": task_id(task_type, symbol, suffix),
        "type": task_type,
        "symbol": symbol,
        "status": status,
        "priority": priority,
        "summary": summary,
        "next_check": next_check,
        "details": details or {},
        "updated_at": now_iso(),
    }


def reconcile_tasks(
    state: dict[str, Any],
    account: dict[str, Any],
    quotes: dict[str, Any],
    config: dict[str, Any],
    trading_date: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tasks: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for order in queued_buy_orders(state):
        symbol = str(order["symbol"])
        task = build_task(
            "queued_buy_monitor",
            symbol,
            "open",
            "high",
            f"Queued buy requires fill/expire/cancel reconciliation before new {symbol} action.",
            "next_valid_market_monitor",
            {
                "order_id": order.get("id"),
                "state": order.get("state"),
                "quantity": order_quantity(order),
                "limit_price": order.get("price"),
                "cumulative_quantity": order.get("cumulative_quantity", 0),
                "if_filled": "verify position and place/confirm protective stop",
                "if_still_queued": "revalidate live price, risk budget, and pending-candidate status",
            },
            suffix=str(order.get("id", ""))[-8:],
        )
        tasks.append(task)

    for position in tracked_positions(state, account):
        symbol = str(position["symbol"])
        quantity = position_quantity(position)
        if quantity <= 0:
            continue
        stops = stop_orders_for_symbol(state, symbol)
        expected_stop = position.get("stop_price")
        if len(stops) == 0:
            status = "risk"
            priority = "critical"
            summary = f"{symbol} has an open position but no confirmed broker-side protective stop in state."
            events.append({"kind": "protective_stop_task_risk", "symbol": symbol, "reason": "missing_stop"})
        elif len(stops) > 1:
            status = "risk"
            priority = "critical"
            summary = f"{symbol} has multiple open protective stops; consolidate to one stop."
            events.append({"kind": "protective_stop_task_risk", "symbol": symbol, "reason": "duplicate_stops"})
        else:
            stop = stops[0]
            stop_quantity = order_quantity(stop)
            stop_price = float(stop.get("stop_price", expected_stop or 0) or 0)
            expected_price = float(expected_stop or stop_price or 0)
            if stop_quantity != quantity:
                status = "risk"
                priority = "critical"
                summary = f"{symbol} protective stop quantity {stop_quantity} does not match position {quantity}."
                events.append({"kind": "protective_stop_task_risk", "symbol": symbol, "reason": "quantity_mismatch"})
            elif expected_price and stop_price < expected_price:
                status = "risk"
                priority = "high"
                summary = f"{symbol} protective stop is below expected stop price."
                events.append({"kind": "protective_stop_task_risk", "symbol": symbol, "reason": "stop_price_below_expected"})
            else:
                status = "ok"
                priority = "normal"
                summary = f"{symbol} has one open protective stop matching tracked position."
        tasks.append(
            build_task(
                "protective_stop_check",
                symbol,
                status,
                priority,
                summary,
                "every_live_monitor",
                {
                    "position_quantity": quantity,
                    "expected_stop_price": expected_stop,
                    "open_stop_count": len(stops),
                    "stop_order_ids": [order.get("id") for order in stops],
                },
            )
        )

        if position.get("target_price"):
            price = quotes.get(symbol, {}).get("price")
            tasks.append(
                build_task(
                    "profit_target_check",
                    symbol,
                    "watch",
                    "normal",
                    f"Monitor {symbol} for partial/full target or trail decision.",
                    "every_live_monitor",
                    {
                        "current_price": price,
                        "partial_target_price": position.get("partial_target_price"),
                        "target_price": position.get("target_price"),
                    },
                )
            )
        if config.get("risk", {}).get("protective_stop_ratchet_enabled", False):
            tasks.append(
                build_task(
                    "stop_ratchet_check",
                    symbol,
                    "watch",
                    "normal",
                    f"Evaluate whether {symbol} stop can be raised without lowering protection.",
                    "every_live_monitor",
                    {
                        "current_price": quotes.get(symbol, {}).get("price"),
                        "current_stop_price": position.get("stop_price"),
                        "highest_price": position.get("highest_price"),
                    },
                )
            )

    for position in tracked_option_positions(state, account):
        symbol = str(position.get("symbol") or position.get("chain_symbol") or "OPTION")
        option_id = str(position.get("option_id") or "")
        tasks.append(
            build_task(
                "option_exit_check",
                symbol,
                "watch",
                "normal",
                f"Monitor long option exit rules for {symbol}.",
                "every_live_monitor",
                {
                    "option_id": option_id,
                    "quantity": position.get("quantity"),
                    "average_price": position.get("average_price"),
                    "expiration_date": position.get("expiration_date"),
                    "quote_available": bool(option_id and quotes.get(option_id)),
                },
                suffix=option_id[-8:],
            )
        )

    state["agentic_tasks"] = tasks
    state["agentic_tasks_updated_at"] = now_iso()
    return tasks, events


def render_tasks_markdown(tasks: list[dict[str, Any]]) -> str:
    lines = ["# Agentic Task Queue", "", f"Updated: {now_iso()}", "", "## Open Tasks", ""]
    if not tasks:
        lines.append("No open tasks.")
        return "\n".join(lines) + "\n"
    for task in tasks:
        lines.append(
            f"- [ ] {task['type']} | {task['symbol']} | {task['status']} | priority={task['priority']}"
        )
        lines.append(f"  - {task['summary']}")
        lines.append(f"  - Next check: {task['next_check']}")
        details = task.get("details") or {}
        for key in sorted(details):
            lines.append(f"  - {key}: {details[key]}")
    return "\n".join(lines) + "\n"


def save_task_files(json_path: Path, md_path: Path, tasks: list[dict[str, Any]]) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump({"tasks": tasks, "updated_at": now_iso()}, handle, indent=2, sort_keys=True)
        handle.write("\n")
    md_path.write_text(render_tasks_markdown(tasks), encoding="utf-8")
