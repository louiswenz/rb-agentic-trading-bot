#!/usr/bin/env python3
"""Convert Robinhood historical bar snapshots into swing scanner price CSVs.

The Robinhood MCP returns historicals as JSON. `swing_strategy.py` expects one
CSV per symbol with Date,Open,High,Low,Close columns. This utility is intentionally
offline and file based: it never calls broker tools and never places orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


REQUIRED_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


def load_snapshot(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    if not isinstance(snapshot, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return snapshot


def historical_results(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    data = snapshot.get("data", snapshot)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ValueError("historical snapshot missing data.results list")
    return [item for item in results if isinstance(item, dict)]


def parse_date(value: str) -> str:
    if not value:
        raise ValueError("bar missing begins_at")
    if "T" not in value:
        return value[:10]
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).date().isoformat()


def parse_float(bar: dict[str, Any], key: str) -> float:
    raw = bar.get(key)
    if raw in (None, ""):
        raise ValueError(f"bar missing {key}")
    return float(raw)


def normalize_bars(item: dict[str, Any]) -> list[dict[str, str]]:
    rows_by_date: dict[str, dict[str, str]] = {}
    for bar in item.get("bars", []):
        if not isinstance(bar, dict) or bar.get("interpolated") is True:
            continue
        date = parse_date(str(bar.get("begins_at", "")))
        rows_by_date[date] = {
            "Date": date,
            "Open": f"{parse_float(bar, 'open_price'):.6f}",
            "High": f"{parse_float(bar, 'high_price'):.6f}",
            "Low": f"{parse_float(bar, 'low_price'):.6f}",
            "Close": f"{parse_float(bar, 'close_price'):.6f}",
            "Volume": f"{float(bar.get('volume', 0) or 0):.0f}",
        }
    return [rows_by_date[date] for date in sorted(rows_by_date)]


def write_symbol_csv(symbol: str, rows: list[dict[str, str]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{symbol.upper()}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def convert(paths: list[Path], output_dir: Path, min_bars: int) -> dict[str, Any]:
    converted: dict[str, int] = {}
    skipped: dict[str, str] = {}

    for path in paths:
        snapshot = load_snapshot(path)
        for item in historical_results(snapshot):
            symbol = str(item.get("symbol", "")).upper().strip()
            if not symbol:
                skipped[f"{path.name}:unknown"] = "missing symbol"
                continue
            try:
                rows = normalize_bars(item)
            except (TypeError, ValueError) as exc:
                skipped[symbol] = str(exc)
                continue
            if len(rows) < min_bars:
                skipped[symbol] = f"only {len(rows)} bars; need at least {min_bars}"
                continue
            write_symbol_csv(symbol, rows, output_dir)
            converted[symbol] = len(rows)

    return {
        "output_dir": str(output_dir),
        "converted": dict(sorted(converted.items())),
        "skipped": dict(sorted(skipped.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert Robinhood historical JSON snapshots to price CSV files.")
    parser.add_argument("snapshots", nargs="+", help="One or more JSON files returned by get_equity_historicals.")
    parser.add_argument("--output-dir", required=True, help="Directory for SYMBOL.csv files.")
    parser.add_argument("--min-bars", type=int, default=201, help="Minimum non-interpolated bars required per symbol.")
    args = parser.parse_args()

    try:
        result = convert([Path(path) for path in args.snapshots], Path(args.output_dir), args.min_bars)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps({"status": "ok", **result}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
