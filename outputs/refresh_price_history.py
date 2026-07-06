#!/usr/bin/env python3
"""Refresh scanner price CSVs from public daily OHLC history.

This utility only writes local price-history files. It does not call broker
order APIs, review orders, or make trading decisions. Live entry validation
should still use broker quotes at scan time.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


CSV_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


YAHOO_SYMBOL_OVERRIDES = {
    "VIX": "^VIX",
}


def yahoo_symbol(symbol: str) -> str:
    return YAHOO_SYMBOL_OVERRIDES.get(symbol.upper(), symbol)


def load_universe(config_path: Path) -> list[str]:
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    strategy = config.get("strategy", {})
    symbols = strategy.get("trade_universe", [])
    market_risk_indicators = strategy.get("market_risk_indicators", [])
    excluded = {str(symbol).upper().strip() for symbol in strategy.get("excluded_symbols", [])}
    if not isinstance(symbols, list):
        raise ValueError("strategy.trade_universe must be a list")
    if not isinstance(market_risk_indicators, list):
        raise ValueError("strategy.market_risk_indicators must be a list when present")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_symbol in [*symbols, *market_risk_indicators]:
        symbol = str(raw_symbol).upper().strip()
        if not symbol or symbol in excluded or symbol in seen:
            continue
        normalized.append(symbol)
        seen.add(symbol)
    return normalized


def yahoo_chart_url(symbol: str, start: datetime, end: datetime) -> str:
    params = urllib.parse.urlencode(
        {
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "false",
        }
    )
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{params}"


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "codex-agentic-history-refresh/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Yahoo response was not a JSON object")
    return data


def rows_from_yahoo_chart(symbol: str, data: dict[str, Any]) -> list[dict[str, str]]:
    chart = data.get("chart", {})
    error = chart.get("error")
    if error:
        raise ValueError(f"Yahoo chart error for {symbol}: {error}")
    results = chart.get("result")
    if not isinstance(results, list) or not results:
        raise ValueError(f"Yahoo chart response missing result for {symbol}")

    result = results[0]
    timestamps = result.get("timestamp")
    quote_items = result.get("indicators", {}).get("quote", [])
    if not isinstance(timestamps, list) or not quote_items:
        raise ValueError(f"Yahoo chart response missing quote bars for {symbol}")
    quote = quote_items[0]

    rows_by_date: dict[str, dict[str, str]] = {}
    for index, ts in enumerate(timestamps):
        try:
            open_price = quote["open"][index]
            high_price = quote["high"][index]
            low_price = quote["low"][index]
            close_price = quote["close"][index]
            volume = quote.get("volume", [None] * len(timestamps))[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Malformed Yahoo quote bars for {symbol}") from exc
        if None in (open_price, high_price, low_price, close_price):
            continue
        date = datetime.fromtimestamp(int(ts), UTC).date().isoformat()
        rows_by_date[date] = {
            "Date": date,
            "Open": f"{float(open_price):.6f}",
            "High": f"{float(high_price):.6f}",
            "Low": f"{float(low_price):.6f}",
            "Close": f"{float(close_price):.6f}",
            "Volume": f"{float(volume):.0f}" if volume is not None else "",
        }
    return [rows_by_date[date] for date in sorted(rows_by_date)]


def write_csv(symbol: str, rows: list[dict[str, str]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / f"{symbol}.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def refresh(
    symbols: list[str],
    output_dir: Path,
    raw_dir: Path | None,
    min_bars: int,
    lookback_days: int,
    timeout: float,
    pause_seconds: float,
) -> dict[str, Any]:
    end = datetime.now(UTC) + timedelta(days=1)
    start = end - timedelta(days=lookback_days)
    converted: dict[str, int] = {}
    skipped: dict[str, str] = {}

    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)

    for symbol in symbols:
        try:
            source_symbol = yahoo_symbol(symbol)
            data = fetch_json(yahoo_chart_url(source_symbol, start, end), timeout)
            if raw_dir:
                (raw_dir / f"yahoo_{symbol}.json").write_text(json.dumps(data), encoding="utf-8")
            rows = rows_from_yahoo_chart(symbol, data)
            if len(rows) < min_bars:
                skipped[symbol] = f"only {len(rows)} bars; need at least {min_bars}"
                continue
            write_csv(symbol, rows, output_dir)
            converted[symbol] = len(rows)
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            skipped[symbol] = str(exc)
        if pause_seconds:
            time.sleep(pause_seconds)

    return {
        "output_dir": str(output_dir),
        "converted": dict(sorted(converted.items())),
        "skipped": dict(sorted(skipped.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh scanner daily price CSVs from Yahoo Finance.")
    parser.add_argument("--config", default="outputs/strategy_config.json")
    parser.add_argument("--output-dir", default="work/agentic_price_history")
    parser.add_argument("--raw-dir", default="work/agentic_price_history_raw")
    parser.add_argument("--symbols", help="Optional comma-separated symbols; defaults to configured universe.")
    parser.add_argument("--min-bars", type=int, default=201)
    parser.add_argument("--lookback-days", type=int, default=420)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--pause-seconds", type=float, default=0.05)
    args = parser.parse_args()

    try:
        symbols = (
            [symbol.upper().strip() for symbol in args.symbols.split(",") if symbol.strip()]
            if args.symbols
            else load_universe(Path(args.config))
        )
        result = refresh(
            symbols=symbols,
            output_dir=Path(args.output_dir),
            raw_dir=Path(args.raw_dir) if args.raw_dir else None,
            min_bars=args.min_bars,
            lookback_days=args.lookback_days,
            timeout=args.timeout,
            pause_seconds=args.pause_seconds,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps({"status": "ok", **result}, indent=2))
    return 0 if result["converted"] else 1


if __name__ == "__main__":
    sys.exit(main())
