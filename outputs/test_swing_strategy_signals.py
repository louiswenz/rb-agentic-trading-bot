#!/usr/bin/env python3
"""Tests for swing strategy signal helpers."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import json
from copy import deepcopy
from datetime import date, timedelta


OUTPUTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(OUTPUTS))

import swing_strategy as scanner  # noqa: E402
from test_swing_strategy_news import make_bars  # noqa: E402


def load_config() -> dict:
    with (OUTPUTS / "strategy_config.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


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
    def setUp(self) -> None:
        self.config = load_config()
        self.config["strategy"]["min_average_dollar_volume"] = 0.0

    def test_volume_ratio_uses_prior_average(self) -> None:
        items = bars(count=22, volume=1000)
        items[-1] = scanner.Bar("2026-02-01", 121, 123, 120, 122, 1500)

        self.assertEqual(scanner.volume_ratio(items, 20), 1.5)

    def test_add_on_position_size_respects_aggregate_symbol_cap(self) -> None:
        shares, position_value, max_loss = scanner.position_size(
            account_value=10000.0,
            settled_cash=5000.0,
            entry=200.0,
            stop=190.0,
            risk_per_trade_pct=2.0,
            max_position_pct=35.0,
            existing_position_value=3300.0,
            min_cash_reserve_pct=15.0,
        )

        self.assertEqual(shares, 1)
        self.assertEqual(position_value, 200.0)
        self.assertEqual(max_loss, 10.0)

        shares, position_value, max_loss = scanner.position_size(
            account_value=10000.0,
            settled_cash=5000.0,
            entry=200.0,
            stop=190.0,
            risk_per_trade_pct=2.0,
            max_position_pct=35.0,
            existing_position_value=3500.0,
            min_cash_reserve_pct=15.0,
        )

        self.assertEqual((shares, position_value, max_loss), (0, 0.0, 0.0))

    def test_usable_position_size_rank_rewards_larger_deployable_size(self) -> None:
        config = deepcopy(self.config)
        config["strategy"]["usable_position_size_rank"] = {
            "enabled": True,
            "rank_weight": 2.0,
            "target_shares": 5,
            "target_position_pct": 20.0,
            "share_count_weight": 0.65,
            "position_value_weight": 0.35,
        }

        small_score = scanner.usable_position_size_score(1, 350.0, 7000.0, config)
        larger_score = scanner.usable_position_size_score(5, 700.0, 7000.0, config)

        self.assertGreater(larger_score, small_score)
        self.assertLessEqual(larger_score, 2.0)

    def test_candidate_rank_includes_usable_position_size_bonus(self) -> None:
        config = deepcopy(self.config)
        stock_bars, spy_bars = make_bars()

        enabled_candidate = scanner.scan_symbol(
            "AMD",
            stock_bars,
            spy_bars,
            account_value=7000.0,
            settled_cash=3000.0,
            config=config,
            news_snapshot={},
        )

        disabled_config = deepcopy(config)
        disabled_config["strategy"]["usable_position_size_rank"]["enabled"] = False
        disabled_candidate = scanner.scan_symbol(
            "AMD",
            stock_bars,
            spy_bars,
            account_value=7000.0,
            settled_cash=3000.0,
            config=disabled_config,
            news_snapshot={},
        )

        self.assertIsNotNone(enabled_candidate)
        self.assertIsNotNone(disabled_candidate)
        assert enabled_candidate is not None
        assert disabled_candidate is not None
        self.assertGreater(enabled_candidate.usable_size_rank_score, 0.0)
        self.assertGreater(enabled_candidate.combined_rank_score, enabled_candidate.signal_rank_score)
        self.assertEqual(disabled_candidate.usable_size_rank_score, 0.0)

    def test_minimum_stock_dollar_volume_filters_illiquid_symbols(self) -> None:
        config = deepcopy(self.config)
        config["strategy"]["min_average_dollar_volume"] = 100_000_000.0
        stock_bars, spy_bars = make_bars()

        candidate = scanner.scan_symbol(
            "AMD",
            stock_bars,
            spy_bars,
            account_value=5000.0,
            settled_cash=5000.0,
            config=config,
            news_snapshot={},
        )

        self.assertIsNone(candidate)

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

    def test_pullback_in_uptrend_can_create_candidate_without_breakout(self) -> None:
        config = deepcopy(self.config)
        config["strategy"]["pullback_in_uptrend"]["max_distance_above_ema_pct"] = 15.0
        config["strategy"]["pullback_in_uptrend"]["rsi_max"] = 80.0
        config["risk"]["min_stop_pct"] = 3.0
        stock_bars: list[scanner.Bar] = []
        spy_bars: list[scanner.Bar] = []
        for index in range(204):
            day = (date(2026, 1, 1) + timedelta(days=index)).isoformat()
            close = 80.0 + index * 0.1
            stock_bars.append(scanner.Bar(day, close - 0.2, close + 0.3, close - 0.3, close, 1000.0))
            spy_close = 100.0 + index * 0.05
            spy_bars.append(scanner.Bar(day, spy_close - 0.2, spy_close + 0.2, spy_close - 0.2, spy_close, 1000.0))
        recent = [101.0, 103.0, 105.0, 103.0, 101.0, 99.0, 100.0, 101.0]
        for offset, close in enumerate(recent):
            day = date(2026, 8, 1) + timedelta(days=offset)
            high = close + 1.0
            if offset == len(recent) - 2:
                high = close + 2.5
            if offset == len(recent) - 1:
                high = close + 0.2
            stock_bars.append(scanner.Bar(day.isoformat(), close - 0.5, high, close - 1.0, close, 900.0))
            spy_close = 110.0 + offset * 0.1
            spy_bars.append(scanner.Bar(day.isoformat(), spy_close - 0.2, spy_close + 0.2, spy_close - 0.2, spy_close, 1000.0))

        candidate = scanner.scan_symbol(
            "AMD",
            stock_bars,
            spy_bars,
            account_value=5000.0,
            settled_cash=5000.0,
            config=config,
            news_snapshot={},
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.setup_type, "pullback_in_uptrend")
        self.assertIn("pullback-in-uptrend", candidate.reason)

    def test_sector_relative_momentum_contributes_to_rank(self) -> None:
        config = deepcopy(self.config)
        stock_bars, spy_bars = make_bars()
        sector_bars = deepcopy(spy_bars)
        for index in range(len(sector_bars)):
            close = 80.0 + index * 0.2
            sector_bars[index] = scanner.Bar(sector_bars[index].date, close - 0.1, close + 0.2, close - 0.2, close, 1000.0)

        candidate = scanner.scan_symbol(
            "AMD",
            stock_bars,
            spy_bars,
            account_value=5000.0,
            settled_cash=5000.0,
            config=config,
            news_snapshot={},
            sector_bars_by_symbol={"SMH": sector_bars},
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertIsNotNone(candidate.sector_relative_strength_pct)
        self.assertGreater(candidate.combined_rank_score, candidate.relative_strength_pct)

    def test_quality_range_reversion_can_pass_without_positive_relative_strength(self) -> None:
        config = deepcopy(self.config)
        config["risk"]["min_stop_pct"] = 1.0
        config["risk"]["max_stop_pct"] = 12.0
        stock_bars: list[scanner.Bar] = []
        spy_bars: list[scanner.Bar] = []
        for index in range(205):
            day = (date(2026, 1, 1) + timedelta(days=index)).isoformat()
            close = 95.0 + index * 0.12
            stock_bars.append(scanner.Bar(day, close - 0.4, close + 0.5, close - 0.5, close, 1000.0))
            spy_close = 100.0 + index * 0.25
            spy_bars.append(scanner.Bar(day, spy_close - 0.2, spy_close + 0.3, spy_close - 0.3, spy_close, 1000.0))
        recent = [121.0, 119.0, 116.0, 113.0, 115.0]
        for offset, close in enumerate(recent):
            day = date(2026, 8, 1) + timedelta(days=offset)
            open_price = close - 1.0 if offset == len(recent) - 1 else close + 0.5
            stock_bars.append(scanner.Bar(day.isoformat(), open_price, close + 0.8, close - 1.2, close, 800.0))
            spy_close = 151.5 + offset * 0.3
            spy_bars.append(scanner.Bar(day.isoformat(), spy_close - 0.2, spy_close + 0.3, spy_close - 0.3, spy_close, 1000.0))

        candidate = scanner.scan_symbol(
            "AMD",
            stock_bars,
            spy_bars,
            account_value=7000.0,
            settled_cash=3000.0,
            config=config,
            news_snapshot={},
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertLess(candidate.relative_strength_pct, 0)
        self.assertEqual(candidate.setup_type, "quality_range_reversion")

    def test_sector_relative_pullback_can_pass_while_stock_lags_sector(self) -> None:
        config = deepcopy(self.config)
        config["risk"]["min_stop_pct"] = 1.0
        config["risk"]["max_stop_pct"] = 12.0
        stock_bars: list[scanner.Bar] = []
        spy_bars: list[scanner.Bar] = []
        sector_bars: list[scanner.Bar] = []
        for index in range(205):
            day = (date(2026, 1, 1) + timedelta(days=index)).isoformat()
            stock_close = 90.0 + index * 0.08
            sector_close = 80.0 + index * 0.16
            spy_close = 100.0 + index * 0.09
            stock_bars.append(scanner.Bar(day, stock_close - 0.3, stock_close + 0.4, stock_close - 0.4, stock_close, 1000.0))
            sector_bars.append(scanner.Bar(day, sector_close - 0.2, sector_close + 0.3, sector_close - 0.3, sector_close, 1000.0))
            spy_bars.append(scanner.Bar(day, spy_close - 0.2, spy_close + 0.3, spy_close - 0.3, spy_close, 1000.0))
        recent = [108.0, 106.0, 104.0, 102.0, 103.5]
        for offset, close in enumerate(recent):
            day = date(2026, 8, 1) + timedelta(days=offset)
            stock_bars.append(scanner.Bar(day.isoformat(), close - 0.8, close + 0.5, close - 1.0, close, 800.0))
            sector_close = 113.0 + offset * 0.4
            sector_bars.append(scanner.Bar(day.isoformat(), sector_close - 0.2, sector_close + 0.3, sector_close - 0.3, sector_close, 1000.0))
            spy_close = 119.0 + offset * 0.05
            spy_bars.append(scanner.Bar(day.isoformat(), spy_close - 0.2, spy_close + 0.3, spy_close - 0.3, spy_close, 1000.0))

        candidate = scanner.scan_symbol(
            "AMD",
            stock_bars,
            spy_bars,
            account_value=7000.0,
            settled_cash=3000.0,
            config=config,
            news_snapshot={},
            sector_bars_by_symbol={"SMH": sector_bars},
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.setup_type, "sector_relative_pullback")
        self.assertLess(candidate.sector_relative_strength_pct or 0, 0)


if __name__ == "__main__":
    unittest.main()
