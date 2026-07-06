#!/usr/bin/env python3
"""Universe and low-account filters for the Agentic swing scanner."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest


OUTPUTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(OUTPUTS))

import swing_strategy  # noqa: E402
from test_swing_strategy_news import make_bars  # noqa: E402


def load_config() -> dict:
    with (OUTPUTS / "strategy_config.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


class StrategyConfigUniverseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.stock_bars, self.spy_bars = make_bars()

    def test_expanded_universe_contains_diverse_lower_priced_names(self) -> None:
        universe = set(self.config["strategy"]["trade_universe"])

        expected = {
            "XLI",
            "XLU",
            "XLB",
            "UBER",
            "PFE",
            "BAC",
            "GM",
            "F",
            "T",
            "VZ",
            "CCL",
            "DAL",
            "OXY",
            "SMH",
            "GLD",
            "TLT",
            "INTC",
            "MU",
            "PLTR",
            "GS",
            "UNH",
            "WMT",
            "CAT",
            "LUV",
            "XOM",
            "FCX",
            "PLD",
        }
        self.assertTrue(expected.issubset(universe))
        self.assertNotIn("SPCH", universe)

        groups = self.config["strategy"]["sector_concentration"]["symbol_groups"]
        missing_groups = sorted(symbol for symbol in universe if symbol not in groups)
        self.assertEqual(missing_groups, [])

    def test_excluded_symbol_cannot_be_candidate(self) -> None:
        candidate = swing_strategy.scan_symbol(
            "SPCH",
            self.stock_bars,
            self.spy_bars,
            account_value=2000.0,
            settled_cash=2000.0,
            config=self.config,
            news_snapshot={},
        )

        self.assertIsNone(candidate)

    def test_under_5000_price_cap_blocks_oversized_symbols(self) -> None:
        expensive_bars = [
            swing_strategy.Bar(bar.date, bar.open * 4, bar.high * 4, bar.low * 4, bar.close * 4)
            for bar in self.stock_bars
        ]

        candidate = swing_strategy.scan_symbol(
            "AMD",
            expensive_bars,
            self.spy_bars,
            account_value=2000.0,
            settled_cash=2000.0,
            config=self.config,
            news_snapshot={},
        )

        self.assertIsNone(candidate)

    def test_hourly_schedule_and_token_efficiency_defaults(self) -> None:
        monitoring = self.config["monitoring"]
        token_efficiency = self.config["token_efficiency"]
        strategy = self.config["strategy"]
        options = self.config["options_strategy"]
        options_exit = self.config["options_exit"]
        execution = self.config["execution"]

        self.assertEqual(monitoring["candidate_scan_times_pt"], ["06:00", "10:00", "17:00"])
        self.assertEqual(monitoring["pending_candidate_validation_time_pt"], "07:00")
        self.assertEqual(monitoring["open_position_poll_seconds"], 3600)
        self.assertEqual(monitoring["elevated_poll_seconds"], 3600)
        self.assertTrue(token_efficiency["deterministic_prescreen_before_news"])
        self.assertEqual(token_efficiency["prescreen_news_symbol_limit"], 6)
        self.assertEqual(token_efficiency["news_cache_ttl_hours"], 48)
        self.assertFalse(strategy["sector_concentration"]["enabled"])
        self.assertTrue(strategy["allow_add_to_existing_positions"])
        self.assertEqual(
            strategy["relaxed_entry"],
            {
                "enabled": True,
                "volume_min_ratio": 0.8,
                "prior_high_tolerance_pct": 0.5,
                "min_relative_strength_pct": -1.0,
                "allow_momentum_continuation": True,
                "momentum_min_relative_strength_pct": 1.0,
                "momentum_min_volume_ratio": 0.8,
            },
        )
        self.assertTrue(execution["allow_options"])
        self.assertTrue(options["enabled"])
        self.assertEqual(options["strategies"], ["long_call", "long_put"])
        self.assertEqual(options["premium_risk_mode"], "full_premium_at_risk")
        self.assertTrue(options_exit["enabled"])
        self.assertEqual(options_exit["profit_target_pct"], 50.0)
        self.assertEqual(options_exit["stop_loss_pct"], 35.0)
        self.assertEqual(options_exit["min_dte_exit"], 14)

    def test_option_intent_for_equity_candidate_uses_long_call_defaults(self) -> None:
        candidate = swing_strategy.scan_symbol(
            "AMD",
            self.stock_bars,
            self.spy_bars,
            account_value=2000.0,
            settled_cash=2000.0,
            config=self.config,
            news_snapshot={},
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        intent = swing_strategy.option_intent_for_candidate(candidate, self.config)
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent["strategy"], "long_call")
        self.assertEqual(intent["option_type"], "call")
        self.assertEqual(intent["min_dte"], 30)
        self.assertEqual(intent["max_dte"], 60)


if __name__ == "__main__":
    unittest.main()
