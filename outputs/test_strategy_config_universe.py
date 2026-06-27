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
        }
        self.assertTrue(expected.issubset(universe))
        self.assertNotIn("SPCH", universe)

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

        self.assertEqual(monitoring["candidate_scan_times_pt"], ["06:00", "10:00", "17:00"])
        self.assertEqual(monitoring["pending_candidate_validation_time_pt"], "07:00")
        self.assertEqual(monitoring["open_position_poll_seconds"], 3600)
        self.assertEqual(monitoring["elevated_poll_seconds"], 3600)
        self.assertTrue(token_efficiency["deterministic_prescreen_before_news"])
        self.assertEqual(token_efficiency["prescreen_news_symbol_limit"], 6)
        self.assertEqual(token_efficiency["news_cache_ttl_hours"], 48)


if __name__ == "__main__":
    unittest.main()
