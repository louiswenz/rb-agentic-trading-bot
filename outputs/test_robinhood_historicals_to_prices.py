#!/usr/bin/env python3
"""Tests for Robinhood historicals to scanner CSV conversion."""

from __future__ import annotations

import csv
import json
import pathlib
import sys
import tempfile
import unittest


OUTPUTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(OUTPUTS))

import robinhood_historicals_to_prices as converter  # noqa: E402


def make_snapshot(symbol: str, count: int = 3) -> dict:
    bars = []
    for index in range(count):
        bars.append(
            {
                "begins_at": f"2026-01-{index + 1:02d}T00:00:00Z",
                "open_price": str(100 + index),
                "high_price": str(102 + index),
                "low_price": str(99 + index),
                "close_price": str(101 + index),
                "volume": 1000 + index,
                "session": "reg",
            }
        )
    return {"data": {"results": [{"symbol": symbol, "interval": "day", "bounds": "regular", "bars": bars}]}}


class RobinhoodHistoricalsToPricesTests(unittest.TestCase):
    def test_convert_writes_scanner_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = pathlib.Path(temp)
            snapshot_path = temp_path / "historicals.json"
            snapshot_path.write_text(json.dumps(make_snapshot("AMD")), encoding="utf-8")

            result = converter.convert([snapshot_path], temp_path / "prices", min_bars=3)

            self.assertEqual(result["converted"], {"AMD": 3})
            csv_path = temp_path / "prices" / "AMD.csv"
            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(
                rows[0],
                {
                    "Date": "2026-01-01",
                    "Open": "100.000000",
                    "High": "102.000000",
                    "Low": "99.000000",
                    "Close": "101.000000",
                    "Volume": "1000",
                },
            )
            self.assertEqual(rows[-1]["Date"], "2026-01-03")

    def test_convert_skips_symbols_without_minimum_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = pathlib.Path(temp)
            snapshot_path = temp_path / "historicals.json"
            snapshot_path.write_text(json.dumps(make_snapshot("QQQ", count=2)), encoding="utf-8")

            result = converter.convert([snapshot_path], temp_path / "prices", min_bars=3)

            self.assertEqual(result["converted"], {})
            self.assertEqual(result["skipped"], {"QQQ": "only 2 bars; need at least 3"})
            self.assertFalse((temp_path / "prices" / "QQQ.csv").exists())

    def test_interpolated_bars_are_ignored(self) -> None:
        snapshot = make_snapshot("SPY", count=4)
        snapshot["data"]["results"][0]["bars"][1]["interpolated"] = True

        with tempfile.TemporaryDirectory() as temp:
            temp_path = pathlib.Path(temp)
            snapshot_path = temp_path / "historicals.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            result = converter.convert([snapshot_path], temp_path / "prices", min_bars=3)

            self.assertEqual(result["converted"], {"SPY": 3})
            with (temp_path / "prices" / "SPY.csv").open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Date"] for row in rows], ["2026-01-01", "2026-01-03", "2026-01-04"])


if __name__ == "__main__":
    unittest.main()
