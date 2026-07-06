#!/usr/bin/env python3
"""Tests for refresh_price_history.py."""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

import refresh_price_history as refresh


def test_load_universe_excludes_symbols() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "strategy": {
                        "trade_universe": ["spy", "SPCH", " xlu "],
                        "excluded_symbols": ["SPCH"],
                    }
                }
            ),
            encoding="utf-8",
        )

        assert refresh.load_universe(path) == ["SPY", "XLU"]


def test_load_universe_includes_market_risk_indicators_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "strategy": {
                        "trade_universe": ["SPY", "QQQ"],
                        "market_risk_indicators": ["VIX", "SPY"],
                        "excluded_symbols": [],
                    }
                }
            ),
            encoding="utf-8",
        )

        assert refresh.load_universe(path) == ["SPY", "QQQ", "VIX"]


def test_yahoo_symbol_maps_vix_index() -> None:
    assert refresh.yahoo_symbol("VIX") == "^VIX"
    assert refresh.yahoo_symbol("AMD") == "AMD"


def test_rows_from_yahoo_chart_skips_empty_bars() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1767225600, 1767312000],
                    "indicators": {
                        "quote": [
                            {
                                "open": [10.0, None],
                                "high": [11.0, None],
                                "low": [9.5, None],
                                "close": [10.5, None],
                                "volume": [123456, None],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }

    assert refresh.rows_from_yahoo_chart("TST", payload) == [
        {
            "Date": "2026-01-01",
            "Open": "10.000000",
            "High": "11.000000",
            "Low": "9.500000",
            "Close": "10.500000",
            "Volume": "123456",
        }
    ]


def test_write_csv() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = [
            {
                "Date": "2026-01-01",
                "Open": "1.000000",
                "High": "2.000000",
                "Low": "0.500000",
                "Close": "1.500000",
                "Volume": "1000",
            }
        ]
        refresh.write_csv("TST", rows, Path(tmp))

        with (Path(tmp) / "TST.csv").open(newline="", encoding="utf-8") as handle:
            assert list(csv.DictReader(handle)) == rows


if __name__ == "__main__":
    test_load_universe_excludes_symbols()
    test_rows_from_yahoo_chart_skips_empty_bars()
    test_write_csv()
    print("ok")
