#!/usr/bin/env python3
"""Tests for swing strategy signal helpers."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import date, timedelta


OUTPUTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(OUTPUTS))

import swing_strategy as scanner  # noqa: E402


def bars(count: int = 60, start: float = 100.0, volume: float = 1000.0) -> list[scanner.Bar]:
    return [
        scanner.Bar(
            date=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            open=start + index,
            high=start + index + 2,
            low=start + index - 2,
            close=start + index + 1,
            volume=volume,
        )
        for index in range(count)
    ]


class SwingStrategySignalTests(unittest.TestCase):
    def test_volume_ratio_uses_prior_average(self) -> None:
        items = bars(count=22, volume=1000)
        items[-1] = scanner.Bar("2026-02-01", 121, 123, 120, 122, 1500)

        self.assertEqual(scanner.volume_ratio(items, 20), 1.5)

    def test_atr_stop_can_tighten_legacy_stop(self) -> None:
        items = bars(count=30)
        latest = items[-1]
        config = {
            "strategy": {"recent_swing_low_days": 10},
            "risk": {
                "initial_stop_pct": 8.0,
                "atr_stop": {"enabled": True, "days": 14, "multiple": 1.0, "mode": "tighter_of_legacy_and_atr"},
            },
        }

        stop, method, atr_value = scanner.candidate_stop(latest, items, config)

        self.assertEqual(method, "tighter_of_percent_recent_low_and_atr")
        self.assertIsNotNone(atr_value)
        self.assertGreater(stop, latest.close * 0.92)

    def test_market_regime_requires_all_configured_indexes_above_sma(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            prices = pathlib.Path(temp)
            for symbol, final_close in {"SPY": 150.0, "QQQ": 80.0}.items():
                with (prices / f"{symbol}.csv").open("w", encoding="utf-8") as handle:
                    handle.write("Date,Open,High,Low,Close,Volume\n")
                    for index in range(60):
                        close = 100 + index if symbol == "SPY" else 100
                        if index == 59:
                            close = final_close
                        day = (date(2026, 1, 1) + timedelta(days=index)).isoformat()
                        handle.write(f"{day},{close},{close},{close},{close},1000\n")
            output = {"messages": []}
            config = {
                "strategy": {
                    "market_filter_sma_days": 50,
                    "market_regime_filter": {
                        "enabled": True,
                        "sma_days": 50,
                        "required_symbols_above_sma": ["SPY", "QQQ"],
                    },
                }
            }

            self.assertFalse(scanner.market_regime_allows_new_buys(output, prices, config))
            self.assertTrue(any("QQQ" in message for message in output["messages"]))

    def test_freshness_validation_reports_short_symbol_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            prices = pathlib.Path(temp)
            for symbol in ["SPY", "ABC"]:
                with (prices / f"{symbol}.csv").open("w", encoding="utf-8") as handle:
                    handle.write("Date,Open,High,Low,Close,Volume\n")
                    rows = 210 if symbol == "SPY" else 2
                    for index in range(rows):
                        day = (date(2026, 1, 1) + timedelta(days=index)).isoformat()
                        handle.write(f"{day},100,101,99,100,1000\n")
            output = {"messages": []}
            config = {
                "data_freshness": {
                    "missing_or_stale_history_policy": "symbol_ineligible_after_refresh",
                    "min_daily_bars": 201,
                    "max_history_age_calendar_days": 1,
                },
                "strategy": {
                    "benchmark_symbol": "SPY",
                    "trade_universe": ["SPY", "ABC"],
                    "excluded_symbols": [],
                },
            }

            self.assertTrue(scanner.validate_fresh_price_history(output, prices, config))
            self.assertTrue(any("ABC: only 2 bars" in message for message in output["messages"]))


if __name__ == "__main__":
    unittest.main()
