#!/usr/bin/env python3
"""News-filter behavior tests for the Agentic swing scanner."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest


OUTPUTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(OUTPUTS))

import swing_strategy  # noqa: E402


def load_config() -> dict:
    with (OUTPUTS / "strategy_config.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def make_bars(symbol_strength: bool = True) -> tuple[list[swing_strategy.Bar], list[swing_strategy.Bar]]:
    spy_bars: list[swing_strategy.Bar] = []
    stock_bars: list[swing_strategy.Bar] = []
    for day in range(204):
        spy_close = 100.0 + day * 0.1
        stock_close = 50.0 + day * (0.22 if symbol_strength else 0.04)
        date = f"2026-01-{(day % 28) + 1:02d}"
        spy_bars.append(swing_strategy.Bar(date, spy_close - 0.2, spy_close + 0.3, spy_close - 0.4, spy_close, 1000.0))
        stock_bars.append(
            swing_strategy.Bar(date, stock_close - 0.5, stock_close + 1.5, stock_close - 1.5, stock_close, 1000.0)
        )

    recent = [94.0, 96.0, 95.0, 97.0, 96.0, 100.0]
    for offset, close in enumerate(recent):
        day = 204 + offset
        date = f"2026-02-{offset + 1:02d}"
        spy_close = 120.4 + offset * 0.1
        high = close + (1.0 if offset < len(recent) - 1 else 0.5)
        stock_volume = 1500.0 if offset == len(recent) - 1 else 1000.0
        stock_bars.append(swing_strategy.Bar(date, close - 0.5, high, close - 2.5, close, stock_volume))
        spy_bars.append(swing_strategy.Bar(date, spy_close - 0.2, spy_close + 0.3, spy_close - 0.4, spy_close, 1000.0))
    return stock_bars, spy_bars


class SwingStrategyNewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.config["strategy"]["min_average_dollar_volume"] = 0.0
        self.stock_bars, self.spy_bars = make_bars()

    def test_missing_news_is_neutral_candidate_still_allowed(self) -> None:
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
        self.assertEqual(candidate.news_score, 0.0)
        self.assertEqual(candidate.news_summary, "no material recent news found")
        self.assertGreater(candidate.combined_rank_score, candidate.relative_strength_pct)

    def test_positive_news_boosts_combined_rank(self) -> None:
        candidate = swing_strategy.scan_symbol(
            "AMD",
            self.stock_bars,
            self.spy_bars,
            account_value=2000.0,
            settled_cash=2000.0,
            config=self.config,
            news_snapshot={"AMD": {"sentiment_score": 2.0, "summary": "analyst upgrade and product launch"}},
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.news_score, 2.0)
        self.assertGreater(candidate.combined_rank_score, candidate.relative_strength_pct)
        self.assertIn("analyst upgrade", candidate.reason)
        self.assertAlmostEqual(candidate.reward_risk_ratio, 2.0)
        self.assertAlmostEqual(candidate.max_entry, candidate.entry * 1.005)

    def test_blocking_news_rejects_candidate(self) -> None:
        candidate = swing_strategy.scan_symbol(
            "AMD",
            self.stock_bars,
            self.spy_bars,
            account_value=2000.0,
            settled_cash=2000.0,
            config=self.config,
            news_snapshot={"AMD": {"sentiment_score": -1.0, "summary": "trading halt announced"}},
        )

        self.assertIsNone(candidate)

    def test_prescreen_defers_blocking_news_until_finalist_review(self) -> None:
        candidate = swing_strategy.scan_symbol(
            "AMD",
            self.stock_bars,
            self.spy_bars,
            account_value=2000.0,
            settled_cash=2000.0,
            config=self.config,
            news_snapshot={"AMD": {"sentiment_score": -3.0, "summary": "trading halt announced"}},
            apply_news_filter=False,
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.news_score, 0.0)
        self.assertEqual(candidate.news_summary, "news not evaluated in deterministic prescreen")

    def test_adverse_news_score_rejects_candidate(self) -> None:
        candidate = swing_strategy.scan_symbol(
            "AMD",
            self.stock_bars,
            self.spy_bars,
            account_value=2000.0,
            settled_cash=2000.0,
            config=self.config,
            news_snapshot={"AMD": {"sentiment_score": -2.5, "summary": "guidance cut after earnings"}},
        )

        self.assertIsNone(candidate)

    def test_earnings_blackout_rejects_candidate(self) -> None:
        candidate = swing_strategy.scan_symbol(
            "AMD",
            self.stock_bars,
            self.spy_bars,
            account_value=2000.0,
            settled_cash=2000.0,
            config=self.config,
            news_snapshot={"AMD": {"sentiment_score": 1.0, "summary": "earnings soon", "days_until_earnings": 2}},
        )

        self.assertIsNone(candidate)

    def test_parse_symbol_set_accepts_csv_and_json(self) -> None:
        self.assertEqual(swing_strategy.parse_symbol_set("dal, AMD "), {"DAL", "AMD"})
        self.assertEqual(swing_strategy.parse_symbol_set('["dal", "AMD"]'), {"DAL", "AMD"})

    def test_adaptive_gap_uses_strong_setup_threshold(self) -> None:
        self.assertEqual(swing_strategy.adaptive_gap_pct(9.0, 1.0, self.config), 1.0)


if __name__ == "__main__":
    unittest.main()
