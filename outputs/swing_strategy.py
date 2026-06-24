#!/usr/bin/env python3
"""Offline reviewed-candidate scanner for the Robinhood Agentic swing plan.

Input data:
  One CSV per symbol in a prices directory, named SYMBOL.csv.
  Required columns: Date, Open, High, Low, Close.

Example:
  python3 swing_strategy.py \
    --prices-dir ./prices \
    --account-value 10000 \
    --settled-cash 10000 \
    --monthly-start-equity 10000

The script never places orders or connects to Robinhood. It emits trade reviews
that still require broker-side review and explicit user confirmation. When the
config enables automatic stop losses and synthetic profit targets, the review
includes the complete exit package to manage after the buy fills.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Candidate:
    symbol: str
    date: str
    entry: float
    max_entry: float
    stop: float
    shares: int
    position_value: float
    max_loss: float
    buying_power_impact: float
    partial_target: float
    profit_target: float
    reward_risk_ratio: float
    sector_group: str
    relative_strength_pct: float
    news_score: float
    combined_rank_score: float
    news_summary: str
    reason: str
    exit_plan: str


ETF_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV"}


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_bars(path: Path) -> list[Bar]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"Date", "Open", "High", "Low", "Close"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")

        bars = [
            Bar(
                date=row["Date"],
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
            )
            for row in reader
            if row.get("Close")
        ]

    bars.sort(key=lambda bar: bar.date)
    return bars


def sma(values: list[float], days: int) -> float | None:
    if len(values) < days:
        return None
    return sum(values[-days:]) / days


def percent_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100.0


def percent_distance(high: float, low: float) -> float:
    if high == 0:
        return 0.0
    return ((high - low) / high) * 100.0


def has_pullback_or_consolidation(bars: list[Bar], lookback: int) -> bool:
    if len(bars) < lookback + 1:
        return False
    recent = bars[-lookback - 1 : -1]
    closes = [bar.close for bar in recent]
    highest = max(closes)
    lowest = min(closes)
    range_pct = percent_change(highest, lowest)
    had_pullback = any(closes[i] < closes[i - 1] for i in range(1, len(closes)))
    had_consolidation = range_pct <= 4.0
    return had_pullback or had_consolidation


def position_size(
    account_value: float,
    settled_cash: float,
    entry: float,
    stop: float,
    risk_per_trade_pct: float,
    max_position_pct: float,
    max_trade_risk_dollars: float | None = None,
) -> tuple[int, float, float]:
    risk_per_share = max(entry - stop, 0.0)
    if risk_per_share <= 0:
        return 0, 0.0, 0.0

    max_risk_dollars = account_value * (risk_per_trade_pct / 100.0)
    if max_trade_risk_dollars is not None:
        max_risk_dollars = min(max_risk_dollars, max_trade_risk_dollars)
    risk_sized_shares = math.floor(max_risk_dollars / risk_per_share)
    cap_sized_shares = math.floor((account_value * (max_position_pct / 100.0)) / entry)
    cash_sized_shares = math.floor(settled_cash / entry)
    shares = max(0, min(risk_sized_shares, cap_sized_shares, cash_sized_shares))
    position_value = shares * entry
    max_loss = shares * risk_per_share
    return shares, position_value, max_loss


def max_next_session_entry(signal_close: float, max_gap_pct: float) -> float:
    return signal_close * (1.0 + max_gap_pct / 100.0)


def r_multiple_target(entry: float, stop: float, multiple: float) -> float:
    return entry + ((entry - stop) * multiple)


def load_news_snapshot(path_or_json: str | None) -> dict[str, Any]:
    if not path_or_json:
        return {}
    path = Path(path_or_json)
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path_or_json)


def parse_symbol_set(value: str | None) -> set[str]:
    if not value:
        return set()
    value = value.strip()
    if not value:
        return set()
    if value.startswith("["):
        symbols = json.loads(value)
    else:
        symbols = value.split(",")
    return {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}


def parse_float_arg(value: str | None, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def symbol_group(symbol: str, config: dict[str, Any]) -> str:
    groups = config["strategy"].get("sector_concentration", {}).get("symbol_groups", {})
    return str(groups.get(symbol.upper(), "other"))


def held_group_counts(held_symbols: set[str], config: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for symbol in held_symbols:
        group = symbol_group(symbol, config)
        counts[group] = counts.get(group, 0) + 1
    return counts


def max_positions_per_group(account_value: float, config: dict[str, Any]) -> int:
    concentration = config["strategy"].get("sector_concentration", {})
    risk = config["risk"]
    if account_value < float(risk["funding_minimum_standard_usd"]):
        return int(concentration.get("max_positions_per_group_under_5000", 1))
    return int(concentration.get("max_positions_per_group_standard", 1))


def earnings_blackout_reason(item: dict[str, Any], signal_date: str, config: dict[str, Any]) -> str | None:
    blackout_days = int(config["strategy"].get("earnings_blackout_days", 0))
    if blackout_days <= 0:
        return None

    if item.get("days_until_earnings") is not None:
        days_until = int(item["days_until_earnings"])
        if 0 <= days_until <= blackout_days:
            return f"earnings within {days_until} trading days"

    next_earnings_date = item.get("next_earnings_date") or item.get("earnings_date")
    if next_earnings_date:
        days_until = (date.fromisoformat(str(next_earnings_date)) - date.fromisoformat(signal_date)).days
        if 0 <= days_until <= blackout_days:
            return f"earnings on {next_earnings_date}"
    return None


def adaptive_gap_pct(relative_strength: float, news_score: float, config: dict[str, Any]) -> float:
    strategy = config["strategy"]
    adaptive = strategy.get("adaptive_gap", {})
    if not adaptive.get("enabled", False):
        return float(strategy["next_session_max_gap_pct"])
    if news_score <= float(strategy.get("news_filter", {}).get("block_below_score", -2.0)):
        return float(adaptive["weak_or_risk_off_pct"])
    if (
        relative_strength >= float(adaptive["strong_relative_strength_pct"])
        and news_score >= float(adaptive["strong_news_score"])
    ):
        return float(adaptive["strong_setup_pct"])
    return float(adaptive["default_pct"])


def normalize_news_item(
    symbol: str,
    news_snapshot: dict[str, Any],
    config: dict[str, Any],
    signal_date: str,
) -> tuple[bool, float, str]:
    news_config = config["strategy"].get("news_filter", {})
    if not news_config.get("enabled", False):
        return True, 0.0, "news filter disabled"

    item = news_snapshot.get(symbol)
    if not item:
        if news_config.get("missing_news_policy", "neutral") == "block":
            return False, 0.0, "missing recent news snapshot"
        return True, 0.0, "no material recent news found"

    summary = str(item.get("summary") or item.get("headline") or "recent news reviewed")
    score = float(item.get("sentiment_score", item.get("score", 0.0)))
    blackout_reason = earnings_blackout_reason(item, signal_date, config)
    if blackout_reason:
        return False, score, f"earnings blackout: {blackout_reason}"
    lower_text = " ".join(
        [
            summary,
            " ".join(str(headline) for headline in item.get("headlines", [])),
            " ".join(str(event) for event in item.get("material_events", [])),
        ]
    ).lower()
    blocking_keywords = [str(keyword).lower() for keyword in news_config.get("blocking_event_keywords", [])]
    has_blocking_keyword = any(keyword in lower_text for keyword in blocking_keywords)
    has_blocking_event = bool(item.get("blocking_event", False)) or has_blocking_keyword
    if has_blocking_event:
        return False, score, f"blocking news: {summary}"
    if score <= float(news_config.get("block_below_score", -2.0)):
        return False, score, f"adverse news score {score:g}: {summary}"
    return True, score, summary


def scan_symbol(
    symbol: str,
    bars: list[Bar],
    spy_bars: list[Bar],
    account_value: float,
    settled_cash: float,
    config: dict[str, Any],
    news_snapshot: dict[str, Any] | None = None,
    max_trade_risk_dollars: float | None = None,
) -> Candidate | None:
    strategy = config["strategy"]
    risk = config["risk"]
    rs_days = int(strategy["relative_strength_days"])
    swing_days = int(strategy["recent_swing_low_days"])

    min_days = max(201, rs_days + 1, swing_days + 1)
    if len(bars) < min_days or len(spy_bars) < rs_days + 1:
        return None

    closes = [bar.close for bar in bars]
    spy_closes = [bar.close for bar in spy_bars]
    latest = bars[-1]
    prior = bars[-2]

    excluded_symbols = {str(item).upper() for item in strategy.get("excluded_symbols", [])}
    if symbol.upper() in excluded_symbols:
        return None

    if account_value < float(risk["funding_minimum_standard_usd"]):
        max_symbol_price = strategy.get("under_5000_max_symbol_price")
        if max_symbol_price is not None and latest.close > float(max_symbol_price):
            return None

    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    if sma50 is None or sma200 is None:
        return None
    if latest.close <= sma50 or latest.close <= sma200:
        return None

    stock_return = percent_change(latest.close, closes[-rs_days - 1])
    spy_return = percent_change(spy_closes[-1], spy_closes[-rs_days - 1])
    relative_strength = stock_return - spy_return
    if relative_strength <= 0:
        return None

    if latest.close <= prior.high:
        return None

    same_day_move = percent_change(latest.close, prior.close)
    if same_day_move > float(strategy["same_day_chase_limit_pct"]):
        return None

    if not has_pullback_or_consolidation(bars, int(strategy["pullback_lookback_days"])):
        return None

    recent_low = min(bar.low for bar in bars[-swing_days:])
    percent_stop = latest.close * (1.0 - float(risk["initial_stop_pct"]) / 100.0)
    stop = min(percent_stop, recent_low)
    stop_distance_pct = percent_distance(latest.close, stop)
    if stop_distance_pct < float(risk["min_stop_pct"]) or stop_distance_pct > float(risk["max_stop_pct"]):
        return None

    news_ok, news_score, news_summary = normalize_news_item(symbol, news_snapshot or {}, config, latest.date)
    if not news_ok:
        return None

    max_entry = max_next_session_entry(latest.close, adaptive_gap_pct(relative_strength, news_score, config))
    shares, position_value, max_loss = position_size(
        account_value=account_value,
        settled_cash=settled_cash,
        entry=latest.close,
        stop=stop,
        risk_per_trade_pct=float(risk["risk_per_trade_pct"]),
        max_position_pct=float(risk["max_position_pct"]),
        max_trade_risk_dollars=max_trade_risk_dollars,
    )
    if shares < 1:
        return None

    partial_target = r_multiple_target(latest.close, stop, float(risk["partial_profit_r_multiple"]))
    profit_target = r_multiple_target(latest.close, stop, float(risk["synthetic_profit_target_r_multiple"]))
    reward_risk_ratio = (profit_target - latest.close) / (latest.close - stop)
    if reward_risk_ratio < float(strategy["min_reward_risk_ratio"]):
        return None
    news_weight = float(strategy.get("news_filter", {}).get("rank_weight", 0.0))
    combined_rank_score = relative_strength + (news_score * news_weight)

    reason = (
        f"above 50/200-day averages, +{relative_strength:.2f}% 20-day RS vs SPY, "
        "close above prior high after pullback/consolidation"
    )
    if strategy.get("news_filter", {}).get("enabled", False):
        reason = f"{reason}; news score {news_score:g} ({news_summary})"
    exit_plan = (
        f"initial stop {stop:.2f}; consider partial profit at "
        f"{partial_target:.2f} (1R); target {profit_target:.2f} "
        f"({float(risk['synthetic_profit_target_r_multiple']):g}R); "
        f"trail remainder by {float(risk['trailing_stop_pct']):g}% or a close below the 20-day average"
    )
    return Candidate(
        symbol=symbol,
        date=latest.date,
        entry=latest.close,
        max_entry=max_entry,
        stop=stop,
        shares=shares,
        position_value=position_value,
        max_loss=max_loss,
        buying_power_impact=position_value,
        partial_target=partial_target,
        profit_target=profit_target,
        reward_risk_ratio=reward_risk_ratio,
        sector_group=symbol_group(symbol, config),
        relative_strength_pct=relative_strength,
        news_score=news_score,
        combined_rank_score=combined_rank_score,
        news_summary=news_summary,
        reason=reason,
        exit_plan=exit_plan,
    )


def drawdown_paused(account_value: float, monthly_start_equity: float, pause_pct: float) -> bool:
    if monthly_start_equity <= 0:
        return False
    drawdown = percent_change(monthly_start_equity, account_value)
    return drawdown >= pause_pct


def add_vix_context(output: dict[str, Any], prices_dir: Path, config: dict[str, Any]) -> None:
    strategy = config["strategy"]
    if "VIX" not in strategy.get("market_risk_indicators", []):
        return

    path = prices_dir / "VIX.csv"
    if not path.exists():
        output["messages"].append("VIX price file missing; volatility context skipped.")
        return

    bars = load_bars(path)
    if not bars:
        output["messages"].append("VIX price file empty; volatility context skipped.")
        return

    latest = bars[-1]
    caution = float(strategy["vix_caution_level"])
    high_risk = float(strategy["vix_high_risk_level"])
    if latest.close >= high_risk:
        output["messages"].append(
            f"VIX context: {latest.close:.2f}, high-risk volatility regime. Candidate sizing still follows configured risk caps."
        )
    elif latest.close >= caution:
        output["messages"].append(
            f"VIX context: {latest.close:.2f}, elevated volatility. Review gap and stop risk carefully."
        )
    else:
        output["messages"].append(f"VIX context: {latest.close:.2f}, below caution threshold.")


def revalidate_candidate(
    candidate: Candidate,
    live_price: float,
    account_value: float,
    settled_cash: float,
    config: dict[str, Any],
    max_trade_risk_dollars: float | None = None,
) -> Candidate | None:
    risk = config["risk"]
    if live_price > candidate.max_entry:
        return None

    shares, position_value, max_loss = position_size(
        account_value=account_value,
        settled_cash=settled_cash,
        entry=live_price,
        stop=candidate.stop,
        risk_per_trade_pct=float(risk["risk_per_trade_pct"]),
        max_position_pct=float(risk["max_position_pct"]),
        max_trade_risk_dollars=max_trade_risk_dollars,
    )
    if shares < 1:
        return None
    partial_target = r_multiple_target(live_price, candidate.stop, float(risk["partial_profit_r_multiple"]))
    profit_target = r_multiple_target(live_price, candidate.stop, float(risk["synthetic_profit_target_r_multiple"]))
    reward_risk_ratio = (profit_target - live_price) / (live_price - candidate.stop)
    if reward_risk_ratio < float(config["strategy"]["min_reward_risk_ratio"]):
        return None

    exit_plan = (
        f"initial stop {candidate.stop:.2f}; consider partial profit at "
        f"{partial_target:.2f} (1R); target {profit_target:.2f} "
        f"({float(risk['synthetic_profit_target_r_multiple']):g}R); "
        f"trail remainder by {float(risk['trailing_stop_pct']):g}% or a close below the 20-day average"
    )

    return Candidate(
        symbol=candidate.symbol,
        date=candidate.date,
        entry=live_price,
        max_entry=candidate.max_entry,
        stop=candidate.stop,
        shares=shares,
        position_value=position_value,
        max_loss=max_loss,
        buying_power_impact=position_value,
        partial_target=partial_target,
        profit_target=profit_target,
        reward_risk_ratio=reward_risk_ratio,
        sector_group=candidate.sector_group,
        relative_strength_pct=candidate.relative_strength_pct,
        news_score=candidate.news_score,
        combined_rank_score=candidate.combined_rank_score,
        news_summary=candidate.news_summary,
        reason=f"{candidate.reason}; next-session live price validated",
        exit_plan=exit_plan,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan for reviewed swing-trade candidates.")
    parser.add_argument("--config", default="strategy_config.json", help="Path to strategy_config.json.")
    parser.add_argument("--prices-dir", required=True, help="Directory containing SYMBOL.csv price files.")
    parser.add_argument("--account-value", type=float, required=True, help="Current account equity.")
    parser.add_argument("--settled-cash", type=float, required=True, help="Settled cash available for buys.")
    parser.add_argument(
        "--monthly-start-equity",
        type=float,
        required=True,
        help="Account equity at the start of the month for drawdown control.",
    )
    parser.add_argument("--positions-count", type=int, default=0, help="Current open position count.")
    parser.add_argument(
        "--held-symbols",
        default="",
        help="Comma-separated symbols or JSON array already held; held symbols are excluded from new-buy candidates.",
    )
    parser.add_argument(
        "--open-risk-dollars",
        default="0",
        help="Current planned open-position risk in dollars; new trades must stay within total open-risk cap.",
    )
    parser.add_argument(
        "--live-prices",
        help="Optional JSON object of next-session live prices, e.g. '{\"QQQ\": 438.25}'. "
        "When provided, candidates above their max entry are rejected and sizing is recalculated.",
    )
    parser.add_argument(
        "--news-json",
        help="Optional latest-news snapshot path or JSON object keyed by symbol. "
        "Each item may include sentiment_score -3..3, summary, headlines, material_events, and blocking_event.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists() and args.config == "strategy_config.json":
        config_path = Path(__file__).with_name("strategy_config.json")
    config = load_config(config_path)
    prices_dir = Path(args.prices_dir)
    strategy = config["strategy"]
    risk = config["risk"]

    output: dict[str, Any] = {
        "status": "ok",
        "account_mask": config["account"]["masked_account"],
        "review_before_orders": True,
        "order_mode": "pending_candidates_only",
        "candidates": [],
        "messages": [],
    }
    live_prices = json.loads(args.live_prices) if args.live_prices else {}
    news_snapshot = load_news_snapshot(args.news_json)
    held_symbols = parse_symbol_set(args.held_symbols)
    open_risk_dollars = parse_float_arg(args.open_risk_dollars)
    if strategy.get("news_filter", {}).get("enabled", False):
        output["messages"].append(
            f"News filter active: latest symbol news is scored over the last "
            f"{strategy['news_filter']['lookback_hours']} hours when a news snapshot is supplied."
        )

    if drawdown_paused(args.account_value, args.monthly_start_equity, float(risk["drawdown_pause_pct"])):
        output["status"] = "paused"
        output["messages"].append("New trades paused: account drawdown limit reached.")
        return emit(output, args.json)

    add_vix_context(output, prices_dir, config)

    max_positions = int(risk["max_positions_standard"])
    if args.account_value < float(risk["funding_minimum_standard_usd"]):
        max_positions = int(risk["max_positions_under_5000"])
        output["messages"].append(f"Under $5k concentrated mode active: max positions set to {max_positions}.")

    if args.positions_count >= max_positions:
        output["status"] = "full"
        output["messages"].append("New trades blocked: maximum open positions reached.")
        return emit(output, args.json)

    total_open_risk_cap = args.account_value * (float(risk["total_open_risk_pct"]) / 100.0)
    remaining_open_risk = total_open_risk_cap - open_risk_dollars
    if remaining_open_risk <= 0:
        output["status"] = "risk_budget_full"
        output["messages"].append("New trades blocked: total open-risk budget reached.")
        return emit(output, args.json)
    output["messages"].append(
        f"Open-risk budget: ${remaining_open_risk:.2f} remaining of ${total_open_risk_cap:.2f} total cap."
    )

    benchmark = strategy["benchmark_symbol"]
    spy_bars = load_bars(prices_dir / f"{benchmark}.csv")
    spy_sma50 = sma([bar.close for bar in spy_bars], int(strategy["market_filter_sma_days"]))
    if spy_sma50 is None or spy_bars[-1].close <= spy_sma50:
        output["status"] = "market_filter_off"
        output["messages"].append("New long trades blocked: SPY is not above its 50-day moving average.")
        return emit(output, args.json)

    symbols = list(strategy["trade_universe"])
    if args.account_value < float(risk["funding_minimum_standard_usd"]) and bool(risk["under_5000_prefer_etfs"]):
        symbols = [symbol for symbol in symbols if symbol in ETF_SYMBOLS]

    candidates: list[Candidate] = []
    slots = max_positions - args.positions_count
    held_groups = held_group_counts(held_symbols, config)
    max_group_positions = max_positions_per_group(args.account_value, config)
    for symbol in symbols:
        if symbol == benchmark:
            continue
        if symbol.upper() in held_symbols:
            output["messages"].append(f"{symbol} skipped: already held.")
            continue
        group = symbol_group(symbol, config)
        concentration = strategy.get("sector_concentration", {})
        if concentration.get("enabled", False) and held_groups.get(group, 0) >= max_group_positions:
            output["messages"].append(f"{symbol} skipped: {group} exposure already at group cap.")
            continue
        path = prices_dir / f"{symbol}.csv"
        if not path.exists():
            output["messages"].append(f"Missing price file for {symbol}; skipped.")
            continue
        candidate = scan_symbol(
            symbol,
            load_bars(path),
            spy_bars,
            args.account_value,
            args.settled_cash,
            config,
            news_snapshot,
            remaining_open_risk,
        )
        if candidate:
            candidates.append(candidate)

    candidates.sort(key=lambda item: item.combined_rank_score, reverse=True)
    if live_prices:
        revalidated: list[Candidate] = []
        for candidate in candidates:
            live_price = live_prices.get(candidate.symbol)
            if live_price is None:
                output["messages"].append(f"No live price for {candidate.symbol}; candidate remains pending.")
                continue
            updated = revalidate_candidate(
                candidate,
                float(live_price),
                args.account_value,
                args.settled_cash,
                config,
                remaining_open_risk,
            )
            if updated:
                revalidated.append(updated)
            else:
                output["messages"].append(
                    f"{candidate.symbol} skipped: live price above max entry or updated sizing failed."
                )
        candidates = revalidated
        output["order_mode"] = "next_session_price_validated"

    limited = candidates[: min(int(strategy["max_candidates"]), slots)]
    output["candidates"] = [
        {
            "symbol": item.symbol,
            "date": item.date,
            "setup": item.reason,
            "signal_entry": round(item.entry, 2) if not live_prices else None,
            "validated_entry": round(item.entry, 2) if live_prices else None,
            "max_next_session_entry": round(item.max_entry, 2),
            "stop": round(item.stop, 2),
            "shares": item.shares,
            "position_value": round(item.position_value, 2),
            "max_loss": round(item.max_loss, 2),
            "buying_power_impact": round(item.buying_power_impact, 2),
            "sector_group": item.sector_group,
            "partial_target": round(item.partial_target, 2),
            "target_price": round(item.profit_target, 2),
            "reward_risk_ratio": round(item.reward_risk_ratio, 2),
            "relative_strength_pct": round(item.relative_strength_pct, 2),
            "news_score": round(item.news_score, 2),
            "news_summary": item.news_summary,
            "combined_rank_score": round(item.combined_rank_score, 2),
            "exit_plan": item.exit_plan,
            "order_instruction": "Use a regular-hours limit order at or below max_next_session_entry after broker review.",
            "protective_stop_order": {
                "enabled": bool(config["execution"]["auto_place_protective_stop_after_buy_fill"]),
                "submit_after_buy_fill": True,
                "side": "sell",
                "symbol": item.symbol,
                "quantity": item.shares,
                "type": config["execution"]["protective_stop_order_type"],
                "stop_price": round(item.stop, 2),
                "time_in_force": config["execution"]["protective_stop_time_in_force"],
                "note": "If the buy partially fills, submit the stop only for the filled quantity.",
            },
            "synthetic_profit_target": {
                "enabled": bool(config["execution"]["auto_monitor_synthetic_profit_target"]),
                "partial_target_price": round(item.partial_target, 2),
                "partial_target_r_multiple": float(risk["partial_profit_r_multiple"]),
                "target_price": round(item.profit_target, 2),
                "target_r_multiple": float(risk["synthetic_profit_target_r_multiple"]),
                "monitoring_mode": risk["profit_target_mode"],
                "order_type": risk["profit_target_order_type"],
                "default_action": risk["profit_target_default_action"],
                "partial_sell_pct": float(risk["profit_target_partial_sell_pct"]),
                "prefer_native_oco_if_available": bool(config["execution"]["prefer_native_oco_if_available"]),
                "allow_independent_full_quantity_stop_and_target": bool(
                    config["execution"]["allow_independent_full_quantity_stop_and_target"]
                ),
                "trigger_flow": [
                    "recheck position quantity and protective stop status",
                    "choose partial sell, full sell, or trail-only from current momentum and risk state",
                    "cancel, reduce, or replace protective stop before any conflicting profit sell",
                    "submit profit limit sell for chosen quantity",
                    "replace or resize protective stop for any remaining shares",
                    "pause new buys and notify if target sell or stop replacement fails",
                ],
            },
            "requires_user_confirmation": bool(config["execution"]["require_explicit_user_confirmation"]),
        }
        for item in limited
    ]
    if not limited:
        output["messages"].append("No candidates passed all strategy rules.")

    return emit(output, args.json)


def emit(output: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(output, indent=2))
        return 0

    print(f"Status: {output['status']}")
    print(f"Account: {output['account_mask']} | Review before orders: yes | Mode: {output['order_mode']}")
    for message in output["messages"]:
        print(f"- {message}")
    for item in output["candidates"]:
        print()
        print(f"{item['symbol']} reviewed candidate for {item['date']}")
        print(f"  Setup: {item['setup']}")
        entry_label = "Validated entry" if item["validated_entry"] is not None else "Signal entry"
        entry_value = item["validated_entry"] if item["validated_entry"] is not None else item["signal_entry"]
        print(
            f"  {entry_label}: {entry_value} | Max next-session entry: "
            f"{item['max_next_session_entry']} | Stop: {item['stop']} | Shares: {item['shares']}"
        )
        print(f"  Position value: ${item['position_value']} | Max loss: ${item['max_loss']}")
        print(f"  Buying-power impact: ${item['buying_power_impact']}")
        print(
            f"  Risk/reward: {item['reward_risk_ratio']}R | "
            f"Partial target: {item['partial_target']} | Full target: {item['target_price']} | "
            f"Group: {item['sector_group']}"
        )
        print(
            f"  Rank inputs: RS {item['relative_strength_pct']}% | "
            f"News {item['news_score']} | Combined {item['combined_rank_score']}"
        )
        print(f"  News: {item['news_summary']}")
        print(f"  Exit plan: {item['exit_plan']}")
        print(f"  Order instruction: {item['order_instruction']}")
        stop_order = item["protective_stop_order"]
        if stop_order["enabled"]:
            print(
                "  Protective stop: "
                f"{stop_order['type']} sell {stop_order['quantity']} {stop_order['symbol']} "
                f"at stop {stop_order['stop_price']} {stop_order['time_in_force'].upper()} "
                "after buy fill"
            )
        target = item["synthetic_profit_target"]
        if target["enabled"]:
            print(
                "  Profit target: "
                f"partial at {target['partial_target_price']} ({target['partial_target_r_multiple']:g}R), "
                f"synthetic monitor at {target['target_price']} "
                f"({target['target_r_multiple']:g}R); action {target['default_action']}"
            )
        if item["requires_user_confirmation"]:
            print("  Order status: requires explicit user confirmation before any placement")
        else:
            print("  Order status: automatic trading authorized by config; broker review still required when available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
