#!/usr/bin/env python3
"""Behavior tests for the Agentic event-driven monitor."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timezone


OUTPUTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(OUTPUTS))

import agentic_monitor  # noqa: E402


def load_config() -> dict:
    with (OUTPUTS / "strategy_config.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def base_account(position: dict | None = None) -> dict:
    positions = [position] if position else []
    return {
        "equity": 2000.0,
        "account_value": 2000.0,
        "buying_power": 2000.0,
        "positions": positions,
    }


def base_candidate(symbol: str = "AMD") -> dict:
    return {
        "symbol": symbol,
        "shares": 10,
        "max_next_session_entry": 105.0,
        "entry": 100.0,
        "stop": 92.0,
        "partial_target": 108.0,
        "target_price": 112.0,
        "max_loss": 80.0,
        "reward_risk_ratio": 1.5,
        "sector_group": "semiconductors",
    }


def base_option_candidate(symbol: str = "AMD") -> dict:
    candidate = base_candidate(symbol)
    candidate.update(
        {
            "asset_type": "option",
            "option_strategy": "long_call",
            "option_type": "call",
            "option_id": "option-1",
            "option_limit_price": 1.0,
            "contracts": 1,
            "max_loss": 100.0,
            "chain_symbol": symbol,
            "underlying_type": "equity",
        }
    )
    return candidate


def base_option_position(symbol: str = "AMD") -> dict:
    return {
        "asset_type": "option",
        "symbol": symbol,
        "chain_symbol": symbol,
        "option_id": "option-1",
        "quantity": 1,
        "average_price": 1.0,
        "option_type": "call",
        "expiration_date": "2026-07-31",
        "underlying_stop_price": 92.0,
    }


class AgenticMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.state = deepcopy(agentic_monitor.DEFAULT_STATE)

    def run_monitor(
        self,
        *,
        state: dict | None = None,
        account: dict | None = None,
        orders: list[dict] | None = None,
        quotes: dict | None = None,
        candidates: list[dict] | None = None,
        mode: str = "pending",
    ) -> agentic_monitor.MonitorResult:
        return agentic_monitor.run_monitor(
            state or deepcopy(self.state),
            self.config,
            account or base_account(),
            orders or [],
            quotes or {},
            candidates or [],
            mode,
            "2026-06-20",
        )

    def test_pending_candidate_creates_auto_buy_action(self) -> None:
        result = self.run_monitor(
            quotes={"AMD": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            candidates=[base_candidate("AMD")],
        )

        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0]["type"], "review_and_place_equity_buy")
        self.assertEqual(result.actions[0]["symbol"], "AMD")
        self.assertEqual(result.actions[0]["limit_price"], 101.0)
        self.assertEqual(result.actions[0]["reward_risk_ratio"], 1.5)
        self.assertEqual(result.actions[0]["estimated_max_loss"], 80.0)
        self.assertTrue(result.actions[0]["after_fill"]["place_protective_stop"])
        self.assertTrue(result.actions[0]["after_fill"]["arm_synthetic_target"])

    def test_daily_buy_cap_blocks_new_buy(self) -> None:
        state = deepcopy(self.state)
        state["trading_date"] = "2026-06-20"
        state["daily_buy_count"] = self.config["execution"]["max_auto_buys_per_day"]

        result = self.run_monitor(
            state=state,
            quotes={"AMD": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            candidates=[base_candidate("AMD")],
        )

        self.assertEqual(result.actions, [])

    def test_option_candidate_blocks_without_account_approval(self) -> None:
        account = base_account()
        account["agentic_allowed"] = True
        account["option_level"] = ""

        result = self.run_monitor(
            account=account,
            quotes={"AMD": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            candidates=[base_option_candidate("AMD")],
        )

        self.assertEqual(result.actions, [])
        self.assertIn("option_trading_not_approved", [item["kind"] for item in result.events])

    def test_option_candidate_creates_buy_to_open_action_when_approved(self) -> None:
        account = base_account()
        account["agentic_allowed"] = True
        account["option_level"] = "option_level_2"

        result = self.run_monitor(
            account=account,
            quotes={"AMD": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            candidates=[base_option_candidate("AMD")],
        )

        self.assertEqual(len(result.actions), 1)
        action = result.actions[0]
        self.assertEqual(action["type"], "review_and_place_option_buy_to_open")
        self.assertEqual(action["chain_symbol"], "AMD")
        self.assertEqual(action["legs"][0]["position_effect"], "open")
        self.assertEqual(action["legs"][0]["side"], "buy")
        self.assertEqual(action["price"], "1.00")
        self.assertEqual(action["estimated_max_loss"], 100.0)
        self.assertIn("auto_option_buy_planned", [item["kind"] for item in result.events])

    def test_option_sell_to_close_action_shape(self) -> None:
        action = agentic_monitor.review_and_place_option_sell_to_close(
            {
                "symbol": "AMD",
                "chain_symbol": "AMD",
                "option_id": "option-1",
                "quantity": 1,
                "option_type": "call",
            },
            1.55,
            self.config,
        )

        self.assertEqual(action["type"], "review_and_place_option_sell_to_close")
        self.assertEqual(action["legs"][0]["side"], "sell")
        self.assertEqual(action["legs"][0]["position_effect"], "close")
        self.assertEqual(action["price"], "1.55")

    def test_cancel_option_order_action_shape(self) -> None:
        action = agentic_monitor.cancel_option_order_action("order-1", "AMD")

        self.assertEqual(action["type"], "cancel_option_order")
        self.assertEqual(action["asset_type"], "option")
        self.assertEqual(action["order_id"], "order-1")

    def test_option_profit_target_plans_sell_to_close(self) -> None:
        state = deepcopy(self.state)
        state["option_positions"] = [base_option_position()]
        account = base_account()
        account["option_level"] = "option_level_2"
        account["option_positions"] = [base_option_position()]

        result = self.run_monitor(
            state=state,
            account=account,
            quotes={
                "AMD": {"price": 101.0},
                "option-1": {"price": 1.55},
                "SPY": {"price": 600.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0]["type"], "review_and_place_option_sell_to_close")
        self.assertEqual(result.actions[0]["exit_reason"], "profit_target")
        self.assertEqual(result.actions[0]["price"], "1.55")
        self.assertIn("option_exit_planned", [item["kind"] for item in result.events])

    def test_option_stop_loss_plans_sell_to_close(self) -> None:
        state = deepcopy(self.state)
        state["option_positions"] = [base_option_position()]
        account = base_account()
        account["option_level"] = "option_level_2"
        account["option_positions"] = [base_option_position()]

        result = self.run_monitor(
            state=state,
            account=account,
            quotes={
                "AMD": {"price": 101.0},
                "option-1": {"price": 0.64},
                "SPY": {"price": 600.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertEqual(result.actions[0]["exit_reason"], "stop_loss")

    def test_option_time_stop_plans_sell_to_close(self) -> None:
        position = base_option_position()
        position["expiration_date"] = "2026-06-30"
        state = deepcopy(self.state)
        state["option_positions"] = [position]
        account = base_account()
        account["option_level"] = "option_level_2"
        account["option_positions"] = [position]

        result = self.run_monitor(
            state=state,
            account=account,
            quotes={
                "AMD": {"price": 101.0},
                "option-1": {"price": 1.01},
                "SPY": {"price": 600.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertEqual(result.actions[0]["exit_reason"], "time_stop")

    def test_option_underlying_stop_plans_sell_to_close(self) -> None:
        state = deepcopy(self.state)
        state["option_positions"] = [base_option_position()]
        account = base_account()
        account["option_level"] = "option_level_2"
        account["option_positions"] = [base_option_position()]

        result = self.run_monitor(
            state=state,
            account=account,
            quotes={
                "AMD": {"price": 91.5},
                "option-1": {"price": 1.01},
                "SPY": {"price": 600.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertEqual(result.actions[0]["exit_reason"], "underlying_stop")

    def test_option_exit_skips_missing_quote(self) -> None:
        state = deepcopy(self.state)
        state["option_positions"] = [base_option_position()]
        account = base_account()
        account["option_level"] = "option_level_2"
        account["option_positions"] = [base_option_position()]

        result = self.run_monitor(
            state=state,
            account=account,
            quotes={"AMD": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            mode="position",
        )

        self.assertEqual(result.actions, [])
        self.assertIn("option_quote_missing", [item["kind"] for item in result.events])

    def test_option_exit_skips_duplicate_close_order(self) -> None:
        state = deepcopy(self.state)
        state["option_positions"] = [base_option_position()]
        account = base_account()
        account["option_level"] = "option_level_2"
        account["option_positions"] = [base_option_position()]

        result = self.run_monitor(
            state=state,
            account=account,
            orders=[
                {
                    "asset_type": "option",
                    "option_id": "option-1",
                    "side": "sell",
                    "position_effect": "close",
                    "state": "confirmed",
                }
            ],
            quotes={
                "AMD": {"price": 101.0},
                "option-1": {"price": 1.55},
                "SPY": {"price": 600.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertEqual(result.actions, [])
        self.assertIn("option_close_order_exists", [item["kind"] for item in result.events])

    def test_option_exit_blocks_without_account_approval(self) -> None:
        state = deepcopy(self.state)
        state["option_positions"] = [base_option_position()]
        account = base_account()
        account["option_level"] = ""
        account["option_positions"] = [base_option_position()]

        result = self.run_monitor(
            state=state,
            account=account,
            quotes={
                "AMD": {"price": 101.0},
                "option-1": {"price": 1.55},
                "SPY": {"price": 600.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertEqual(result.actions, [])
        self.assertIn("option_exit_not_approved", [item["kind"] for item in result.events])

    def test_buy_fill_arms_stop_and_synthetic_target(self) -> None:
        events, actions = agentic_monitor.handle_buy_fill(
            self.state,
            {"symbol": "AMD", "quantity": 10, "price": 100.0, "stop_price": 92.0},
            self.config,
        )

        self.assertEqual(self.state["daily_buy_count"], 1)
        self.assertAlmostEqual(self.state["position"]["target_price"], 112.0)
        self.assertAlmostEqual(self.state["position"]["partial_target_price"], 108.0)
        self.assertEqual(events[0]["kind"], "buy_filled")
        self.assertEqual(actions[0]["type"], "place_protective_stop")
        self.assertEqual(actions[0]["quantity_scope"], "full_position")
        self.assertTrue(actions[0]["consolidate_by_symbol"])
        self.assertTrue(actions[0]["cancel_existing_symbol_stops_first"])
        self.assertEqual(actions[1]["type"], "arm_synthetic_profit_target")

    def test_target_reached_with_high_vix_plans_full_profit_sell_and_elevated_poll(self) -> None:
        position = {
            "symbol": "AMD",
            "quantity": 10,
            "entry_price": 100.0,
            "stop_price": 92.0,
            "target_price": 112.0,
        }

        result = self.run_monitor(
            account=base_account(position),
            quotes={
                "AMD": {"price": 113.0, "trend_score": 0.9},
                "SPY": {"price": 600.0, "trend_score": 1.0},
                "VIX": {"price": 26.0},
            },
            mode="position",
        )

        self.assertEqual(result.next_poll_seconds, 3600)
        self.assertIn("target_reached", [item["kind"] for item in result.events])
        self.assertIn("profit_action_chosen", [item["kind"] for item in result.events])
        self.assertEqual(result.actions[0]["type"], "cancel_or_reduce_protective_stop")
        self.assertEqual(result.actions[1]["type"], "place_profit_limit_sell")
        self.assertEqual(result.actions[1]["quantity"], 10)
        self.assertEqual(result.actions[2]["type"], "replace_stop_for_remaining_quantity")

    def test_open_position_without_event_uses_normal_poll(self) -> None:
        position = {
            "symbol": "AMD",
            "quantity": 10,
            "entry_price": 100.0,
            "stop_price": 92.0,
            "target_price": 112.0,
        }

        result = self.run_monitor(
            account=base_account(position),
            quotes={
                "AMD": {"price": 101.0, "trend_score": 0.3},
                "SPY": {"price": 600.0, "trend_score": 1.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertEqual(result.next_poll_seconds, 3600)
        self.assertEqual(result.actions, [])

    def test_profitable_position_ratchets_stop_to_breakeven(self) -> None:
        position = {
            "symbol": "AMD",
            "quantity": 10,
            "entry_price": 100.0,
            "initial_stop_price": 92.0,
            "stop_price": 92.0,
            "target_price": 112.0,
        }

        result = self.run_monitor(
            account=base_account(position),
            orders=[
                {
                    "id": "stop-1",
                    "symbol": "AMD",
                    "side": "sell",
                    "trigger": "stop",
                    "state": "confirmed",
                    "quantity": 10,
                    "stop_price": 92.0,
                }
            ],
            quotes={
                "AMD": {"price": 106.0, "trend_score": 0.3},
                "SPY": {"price": 600.0, "trend_score": 1.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertIn("protective_stop_ratchet_planned", [item["kind"] for item in result.events])
        self.assertEqual(result.actions[0]["type"], "raise_or_replace_protective_stop")
        self.assertEqual(result.actions[0]["new_stop_price"], 100.0)
        self.assertEqual(result.actions[0]["protective_stop_order_id"], "stop-1")

    def test_profitable_position_trails_from_high_watermark(self) -> None:
        position = {
            "symbol": "AMD",
            "quantity": 10,
            "entry_price": 100.0,
            "initial_stop_price": 92.0,
            "stop_price": 92.0,
            "target_price": 120.0,
            "highest_price": 112.0,
        }

        result = self.run_monitor(
            account=base_account(position),
            orders=[
                {
                    "id": "stop-1",
                    "symbol": "AMD",
                    "side": "sell",
                    "trigger": "stop",
                    "state": "confirmed",
                    "quantity": 10,
                    "stop_price": 92.0,
                }
            ],
            quotes={
                "AMD": {"price": 110.0, "trend_score": 0.8},
                "SPY": {"price": 600.0, "trend_score": 1.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertEqual(result.actions[0]["type"], "raise_or_replace_protective_stop")
        self.assertEqual(result.actions[0]["new_stop_price"], 103.04)
        self.assertEqual(result.actions[0]["reason"], "trailing_high_watermark")

    def test_stop_ratchet_skips_first_thirty_minutes_after_entry(self) -> None:
        position = {
            "symbol": "AMD",
            "quantity": 10,
            "entry_price": 100.0,
            "initial_stop_price": 92.0,
            "stop_price": 92.0,
            "target_price": 120.0,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }

        result = self.run_monitor(
            account=base_account(position),
            orders=[
                {
                    "id": "stop-1",
                    "symbol": "AMD",
                    "side": "sell",
                    "trigger": "stop",
                    "state": "confirmed",
                    "quantity": 10,
                    "stop_price": 92.0,
                }
            ],
            quotes={
                "AMD": {"price": 110.0, "trend_score": 0.8},
                "SPY": {"price": 600.0, "trend_score": 1.0},
                "VIX": {"price": 16.0},
            },
            mode="position",
        )

        self.assertEqual(result.actions, [])

    def test_manual_activity_pauses_new_buys(self) -> None:
        result = self.run_monitor(
            orders=[{"id": "manual-1", "source": "manual", "state": "filled"}],
            quotes={"AMD": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            candidates=[base_candidate("AMD")],
        )

        self.assertEqual(result.state["status"], "paused")
        self.assertEqual(result.state["paused_reason"], "manual_activity_detected")
        self.assertEqual(result.actions, [])

    def test_same_sector_candidate_allowed_when_group_cap_disabled(self) -> None:
        position = {
            "symbol": "NVDA",
            "quantity": 1,
            "entry_price": 100.0,
            "stop_price": 92.0,
        }

        result = self.run_monitor(
            account=base_account(position),
            quotes={"AMD": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            candidates=[base_candidate("AMD")],
        )

        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0]["symbol"], "AMD")
        self.assertNotIn("candidate_group_cap", [item["kind"] for item in result.events])

    def test_held_symbol_add_on_allowed_when_max_positions_reached(self) -> None:
        account = base_account()
        account["positions"] = [
            {"symbol": "DAL", "quantity": 8, "entry_price": 86.24, "stop_price": 87.28},
            {"symbol": "WFC", "quantity": 15, "entry_price": 84.34, "stop_price": 77.63},
        ]
        candidate = base_candidate("DAL")
        candidate["sector_group"] = "airlines"
        candidate["max_loss"] = 10.0

        result = self.run_monitor(
            account=account,
            quotes={"DAL": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            candidates=[candidate],
        )

        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0]["symbol"], "DAL")
        self.assertNotIn("candidate_already_held", [item["kind"] for item in result.events])

    def test_new_symbol_still_blocked_when_max_positions_reached(self) -> None:
        account = base_account()
        account["positions"] = [
            {"symbol": "DAL", "quantity": 8, "entry_price": 86.24, "stop_price": 87.28},
            {"symbol": "WFC", "quantity": 15, "entry_price": 84.34, "stop_price": 77.63},
        ]

        result = self.run_monitor(
            account=account,
            quotes={"AMD": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            candidates=[base_candidate("AMD")],
        )

        self.assertEqual(result.actions, [])
        self.assertIn("max_positions_reached", [item["kind"] for item in result.events])

    def test_total_open_risk_cap_blocks_candidate(self) -> None:
        position = {
            "symbol": "DAL",
            "quantity": 8,
            "entry_price": 86.24,
            "stop_price": 76.4,
        }
        candidate = base_candidate("BAC")
        candidate["sector_group"] = "financials"
        candidate["max_loss"] = 60.0

        result = self.run_monitor(
            account=base_account(position),
            quotes={"BAC": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            candidates=[candidate],
        )

        self.assertEqual(result.actions, [])
        self.assertIn("candidate_open_risk_skip", [item["kind"] for item in result.events])

    def test_queued_buy_order_creates_monitor_task_without_stop_action(self) -> None:
        state = deepcopy(self.state)
        queued_order = {
            "id": "order-rtx",
            "symbol": "RTX",
            "side": "buy",
            "type": "limit",
            "state": "queued",
            "quantity": 3,
            "price": 198.2,
            "cumulative_quantity": 0,
        }

        result = self.run_monitor(
            state=state,
            account=base_account(),
            orders=[queued_order],
            quotes={"RTX": {"price": 198.2}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            mode="position",
        )

        tasks = result.state["agentic_tasks"]
        queued_tasks = [item for item in tasks if item["type"] == "queued_buy_monitor"]
        self.assertEqual(len(queued_tasks), 1)
        self.assertEqual(queued_tasks[0]["symbol"], "RTX")
        self.assertEqual(result.actions, [])

    def test_missing_protective_stop_creates_critical_task(self) -> None:
        position = {
            "symbol": "AMD",
            "quantity": 10,
            "entry_price": 100.0,
            "stop_price": 92.0,
        }

        result = self.run_monitor(
            account=base_account(position),
            orders=[],
            quotes={"AMD": {"price": 101.0}, "SPY": {"price": 600.0}, "VIX": {"price": 16.0}},
            mode="position",
        )

        stop_tasks = [item for item in result.state["agentic_tasks"] if item["type"] == "protective_stop_check"]
        self.assertEqual(len(stop_tasks), 1)
        self.assertEqual(stop_tasks[0]["status"], "risk")
        self.assertEqual(stop_tasks[0]["priority"], "critical")
        self.assertIn("protective_stop_task_risk", [item["kind"] for item in result.events])


if __name__ == "__main__":
    unittest.main()
