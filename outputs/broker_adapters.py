"""Broker adapter interfaces for the Agentic live trading bot.

The mock adapter is safe for local tests. The Robinhood adapter is intentionally
not implemented here because this workspace script cannot directly call Codex's
in-chat Robinhood MCP tools.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol


class BrokerAdapter(Protocol):
    def get_account_snapshot(self) -> dict[str, Any]:
        ...

    def get_orders_snapshot(self) -> list[dict[str, Any]]:
        ...

    def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        ...

    def execute_action(self, action: dict[str, Any]) -> dict[str, Any]:
        ...


DEFAULT_MOCK_STATE: dict[str, Any] = {
    "account": {"equity": 2000.0, "buying_power": 2000.0, "positions": []},
    "orders": [],
    "quotes": {
        "SPY": {"price": 600.0, "trend_score": 1.0},
        "VIX": {"price": 18.0},
    },
}


class MockBrokerAdapter:
    """Paper adapter that persists account/orders/quotes in a JSON file."""

    def __init__(self, path: Path, persist: bool = True):
        self.path = path
        self.persist = persist
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return deepcopy(DEFAULT_MOCK_STATE)
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        merged = deepcopy(DEFAULT_MOCK_STATE)
        merged.update(data)
        return merged

    def _save(self) -> None:
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def get_account_snapshot(self) -> dict[str, Any]:
        return deepcopy(self.data["account"])

    def get_orders_snapshot(self) -> list[dict[str, Any]]:
        return deepcopy(self.data.get("orders", []))

    def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        quotes = self.data.get("quotes", {})
        return {symbol: deepcopy(quotes[symbol]) for symbol in symbols if symbol in quotes}

    def execute_action(self, action: dict[str, Any]) -> dict[str, Any]:
        action_type = action["type"]
        if action_type == "review_and_place_equity_buy":
            return self._paper_buy(action)
        if action_type == "place_protective_stop":
            return self._paper_order(action, state="open")
        if action_type == "arm_synthetic_profit_target":
            return {"status": "armed", "action": action}
        if action_type in {
            "cancel_or_reduce_protective_stop",
            "replace_stop_for_remaining_quantity",
            "raise_or_maintain_trailing_stop",
            "raise_or_replace_protective_stop",
        }:
            return {"status": "ok", "action": action}
        if action_type == "place_profit_limit_sell":
            return self._paper_sell(action)
        return {"status": "ignored", "action": action}

    def _paper_order(self, action: dict[str, Any], state: str) -> dict[str, Any]:
        order = {
            "id": f"mock-{len(self.data.get('orders', [])) + 1}",
            "source": "agentic",
            "state": state,
            **action,
        }
        self.data.setdefault("orders", []).append(order)
        self._save()
        return {"status": state, "order": order}

    def _paper_buy(self, action: dict[str, Any]) -> dict[str, Any]:
        quantity = int(action["quantity"])
        price = float(action["limit_price"])
        cost = quantity * price
        account = self.data["account"]
        if cost > float(account.get("buying_power", 0.0)):
            return {"status": "rejected", "reason": "insufficient_buying_power", "action": action}

        account["buying_power"] = round(float(account["buying_power"]) - cost, 2)
        positions = account.setdefault("positions", [])
        positions[:] = [
            {
                "symbol": action["symbol"],
                "quantity": quantity,
                "entry_price": price,
                "stop_price": round(price * 0.92, 2),
                "target_price": round(price * 1.12, 2),
            }
        ]
        order = {
            "id": f"mock-{len(self.data.get('orders', [])) + 1}",
            "source": "agentic",
            "state": "filled",
            **action,
        }
        self.data.setdefault("orders", []).append(order)
        self._save()
        return {
            "status": "filled",
            "order": order,
            "fill": {
                "symbol": action["symbol"],
                "quantity": quantity,
                "price": price,
                "stop_price": round(price * 0.92, 2),
            },
        }

    def _paper_sell(self, action: dict[str, Any]) -> dict[str, Any]:
        quantity = int(action["quantity"])
        price = float(action["limit_price"])
        account = self.data["account"]
        positions = account.get("positions", [])
        if not positions:
            return {"status": "rejected", "reason": "no_position", "action": action}

        position = positions[0]
        sell_quantity = min(quantity, int(position["quantity"]))
        account["buying_power"] = round(float(account["buying_power"]) + sell_quantity * price, 2)
        remaining = int(position["quantity"]) - sell_quantity
        if remaining <= 0:
            account["positions"] = []
        else:
            position["quantity"] = remaining
        order = {
            "id": f"mock-{len(self.data.get('orders', [])) + 1}",
            "source": "agentic",
            "state": "filled",
            **action,
            "quantity": sell_quantity,
        }
        self.data.setdefault("orders", []).append(order)
        self._save()
        return {"status": "filled", "order": order, "fill": {"symbol": action["symbol"], "quantity": sell_quantity}}


class RobinhoodAdapter:
    """Placeholder for a real broker adapter.

    A real implementation must fetch account/orders/quotes and execute actions
    through approved Robinhood tools or an official broker API. It must preserve
    the broker-review and guardrail behavior from strategy_config.json.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "RobinhoodAdapter is not wired in this workspace. Use mock mode or provide a broker adapter."
        )
