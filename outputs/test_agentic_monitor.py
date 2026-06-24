#!/usr/bin/env python3
"""Behavior tests for the Agentic event-driven monitor."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from copy import deepcopy


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

        self.assertEqual(result.next_poll_seconds, 900)
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

        self.assertEqual(result.next_poll_seconds, 900)
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

    def test_group_cap_blocks_second_position_in_same_sector(self) -> None:
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

        self.assertEqual(result.actions, [])
        self.assertIn("candidate_group_cap", [item["kind"] for item in result.events])

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


if __name__ == "__main__":
    unittest.main()
