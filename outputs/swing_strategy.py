#!/usr/bin/env python3
"""Offline reviewed-candidate scanner for the Robinhood Agentic swing plan.

Input data:
  One CSV per symbol in a prices directory, named SYMBOL.csv.
  Required columns: Date, Open, High, Low, Close. Optional: Volume.

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
    volume: float | None = None


@dataclass(frozen=True)
class Candidate:
    symbol: str
    date: str
    setup_type: str
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
    sector_relative_strength_pct: float | None
    sector_momentum_pct: float | None
    volume_ratio: float | None
    atr: float | None
    stop_method: str
    news_score: float
    combined_rank_score: float
    signal_rank_score: float
    usable_size_rank_score: float
    news_summary: str
    reason: str
    exit_plan: str


@dataclass(frozen=True)
class SetupSignal:
    setup_type: str
    score: float
    reason: str
    partial_target_r_multiple: float
    target_r_multiple: float
    max_gap_pct: float | None = None


ETF_SYMBOLS = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "XLK",
    "XLF",
    "XLE",
    "XLV",
    "SMH",
    "XRT",
    "GLD",
    "TLT",
    "HYG",
    "ARKK",
    "XLI",
    "XLY",
    "XLP",
    "XLU",
    "XLRE",
    "XLC",
    "XLB",
}


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
                volume=float(row["Volume"]) if row.get("Volume") not in (None, "") else None,
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


def average(values: list[float], days: int) -> float | None:
    if len(values) < days:
        return None
    return sum(values[-days:]) / days


def ema(values: list[float], days: int) -> float | None:
    if len(values) < days:
        return None
    multiplier = 2.0 / (days + 1)
    value = sum(values[:days]) / days
    for item in values[days:]:
        value = (item * multiplier) + (value * (1.0 - multiplier))
    return value


def rsi(values: list[float], days: int = 14) -> float | None:
    if len(values) < days + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for index in range(-days, 0):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / days
    avg_loss = sum(losses) / days
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def true_ranges(bars: list[Bar]) -> list[float]:
    ranges: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            ranges.append(bar.high - bar.low)
            continue
        previous_close = bars[index - 1].close
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))
    return ranges


def atr(bars: list[Bar], days: int) -> float | None:
    if len(bars) < days + 1:
        return None
    return average(true_ranges(bars), days)


def volume_ratio(bars: list[Bar], days: int) -> float | None:
    if len(bars) < days + 1 or bars[-1].volume is None:
        return None
    prior_volumes = [bar.volume for bar in bars[-days - 1 : -1] if bar.volume is not None]
    if len(prior_volumes) < days:
        return None
    avg_volume = sum(prior_volumes) / days
    if avg_volume <= 0:
        return None
    return bars[-1].volume / avg_volume


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


def relaxed_entry_config(config: dict[str, Any]) -> dict[str, Any]:
    return config["strategy"].get("relaxed_entry", {})


def relaxed_entry_enabled(config: dict[str, Any]) -> bool:
    return bool(relaxed_entry_config(config).get("enabled", False))


def minimum_relative_strength(config: dict[str, Any]) -> float:
    relaxed = relaxed_entry_config(config)
    if relaxed_entry_enabled(config) and "min_relative_strength_pct" in relaxed:
        return float(relaxed["min_relative_strength_pct"])
    return 0.0


def prior_high_breakout_ok(latest: Bar, prior: Bar, config: dict[str, Any]) -> bool:
    if latest.close > prior.high:
        return True
    relaxed = relaxed_entry_config(config)
    if not relaxed_entry_enabled(config):
        return False
    tolerance_pct = float(relaxed.get("prior_high_tolerance_pct", 0.0))
    if tolerance_pct <= 0:
        return False
    return latest.close >= prior.high * (1.0 - tolerance_pct / 100.0)


def momentum_continuation_ok(
    latest: Bar,
    prior: Bar,
    sma50: float,
    sma200: float,
    relative_strength: float,
    latest_volume_ratio: float | None,
    config: dict[str, Any],
) -> bool:
    relaxed = relaxed_entry_config(config)
    if not relaxed_entry_enabled(config) or not bool(relaxed.get("allow_momentum_continuation", False)):
        return False
    min_rs = float(relaxed.get("momentum_min_relative_strength_pct", max(0.0, minimum_relative_strength(config))))
    min_volume = float(relaxed.get("momentum_min_volume_ratio", relaxed.get("volume_min_ratio", 0.0)))
    if latest.close <= prior.close:
        return False
    if latest.close <= sma50 or latest.close <= sma200:
        return False
    if relative_strength < min_rs:
        return False
    if latest_volume_ratio is not None and latest_volume_ratio < min_volume:
        return False
    return True


def pullback_in_uptrend_ok(
    bars: list[Bar],
    latest: Bar,
    prior: Bar,
    closes: list[float],
    sma50: float,
    sma200: float,
    relative_strength: float,
    latest_volume_ratio: float | None,
    config: dict[str, Any],
) -> bool:
    pullback = config["strategy"].get("pullback_in_uptrend", {})
    if not pullback.get("enabled", False):
        return False

    if latest.close <= sma50 or latest.close <= sma200:
        return False
    if relative_strength < float(pullback.get("min_relative_strength_pct", 0.0)):
        return False

    ema_days = int(pullback.get("ema_days", 20))
    moving_average = ema(closes, ema_days) or sma(closes, ema_days)
    if moving_average is None:
        return False

    lookback = int(pullback.get("lookback_days", 8))
    if len(bars) < lookback + 2:
        return False
    recent = bars[-lookback - 1 : -1]
    recent_high = max(bar.close for bar in recent)
    pullback_pct = percent_change(recent_high, min(bar.low for bar in bars[-lookback:]))
    if pullback_pct < float(pullback.get("min_pullback_pct", 1.5)):
        return False
    if pullback_pct > float(pullback.get("max_pullback_pct", 8.0)):
        return False

    max_distance = float(pullback.get("max_distance_above_ema_pct", 3.0))
    if latest.close > moving_average * (1.0 + max_distance / 100.0):
        return False
    if latest.low > moving_average * (1.0 + max_distance / 100.0):
        return False

    rsi_value = rsi(closes, int(pullback.get("rsi_days", 14)))
    if rsi_value is not None:
        if rsi_value < float(pullback.get("rsi_min", 40.0)) or rsi_value > float(pullback.get("rsi_max", 65.0)):
            return False

    min_volume = float(pullback.get("min_volume_ratio", 0.7))
    if latest_volume_ratio is not None and latest_volume_ratio < min_volume:
        return False

    trigger = str(pullback.get("entry_trigger", "reclaim_prior_high_or_ema"))
    if trigger == "reclaim_ema":
        return latest.close > moving_average and latest.close > prior.close
    return (latest.close > prior.high or latest.close > moving_average) and latest.close > prior.close


def sector_relative_metrics(
    symbol: str,
    bars: list[Bar],
    sector_bars_by_symbol: dict[str, list[Bar]],
    config: dict[str, Any],
) -> tuple[float | None, float | None]:
    sector_config = config["strategy"].get("sector_relative_momentum", {})
    if not sector_config.get("enabled", False):
        return None, None

    proxy_map = sector_config.get("group_proxy_symbols", {})
    proxy_symbol = proxy_map.get(symbol_group(symbol, config))
    if not proxy_symbol:
        return None, None
    proxy_bars = sector_bars_by_symbol.get(str(proxy_symbol).upper())
    days = int(sector_config.get("lookback_days", config["strategy"]["relative_strength_days"]))
    if not proxy_bars or len(proxy_bars) < days + 1 or len(bars) < days + 1:
        return None, None

    stock_return = percent_change(bars[-1].close, bars[-days - 1].close)
    sector_return = percent_change(proxy_bars[-1].close, proxy_bars[-days - 1].close)
    return stock_return - sector_return, sector_return


def recent_drawdown_pct(bars: list[Bar], lookback: int) -> float | None:
    if len(bars) < lookback + 1:
        return None
    recent_high = max(bar.high for bar in bars[-lookback - 1 : -1])
    if recent_high <= 0:
        return None
    return percent_change(recent_high, bars[-1].close)


def bullish_reversal_ok(latest: Bar, prior: Bar) -> bool:
    return latest.close > prior.close and latest.close > latest.open


def setup_exit_multiples(setup_type: str, config: dict[str, Any]) -> tuple[float, float]:
    defaults = config["risk"]
    setup_exits = config["strategy"].get("setup_exit_profiles", {})
    profile = setup_exits.get(setup_type, {})
    partial = float(profile.get("partial_profit_r_multiple", defaults["partial_profit_r_multiple"]))
    target = float(profile.get("target_r_multiple", defaults["synthetic_profit_target_r_multiple"]))
    return partial, target


def setup_weight(setup_type: str, config: dict[str, Any]) -> float:
    return float(config["strategy"].get("setup_rank_weights", {}).get(setup_type, 1.0))


def usable_position_size_score(shares: int, position_value: float, account_value: float, config: dict[str, Any]) -> float:
    """Return a bounded rank bonus for candidates that can deploy meaningful size."""
    rank_config = config["strategy"].get("usable_position_size_rank", {})
    if not rank_config.get("enabled", False):
        return 0.0
    if shares <= 0 or position_value <= 0 or account_value <= 0:
        return 0.0

    target_shares = max(1.0, float(rank_config.get("target_shares", 5)))
    target_position_pct = max(0.01, float(rank_config.get("target_position_pct", 20.0)))
    target_position_value = account_value * (target_position_pct / 100.0)
    share_count_weight = float(rank_config.get("share_count_weight", 0.65))
    position_value_weight = float(rank_config.get("position_value_weight", 0.35))
    weight_total = share_count_weight + position_value_weight
    if weight_total <= 0:
        return 0.0

    share_component = min(shares / target_shares, 1.0)
    value_component = min(position_value / target_position_value, 1.0)
    normalized = ((share_component * share_count_weight) + (value_component * position_value_weight)) / weight_total
    return normalized * float(rank_config.get("rank_weight", 2.0))


def setup_enabled(setup_type: str, config: dict[str, Any]) -> bool:
    setup_config = config["strategy"].get(setup_type, {})
    return bool(setup_config.get("enabled", True))


def evaluate_momentum_breakout(
    bars: list[Bar],
    latest: Bar,
    prior: Bar,
    sma50: float,
    sma200: float,
    relative_strength: float,
    latest_volume_ratio: float | None,
    config: dict[str, Any],
) -> SetupSignal | None:
    if not setup_enabled("momentum_breakout", config):
        return None
    strategy = config["strategy"]
    if latest.close <= sma50 or latest.close <= sma200:
        return None
    if relative_strength < minimum_relative_strength(config):
        return None
    min_volume = float(strategy.get("volume_confirmation", {}).get("min_ratio", 1.2))
    if relaxed_entry_enabled(config):
        min_volume = float(relaxed_entry_config(config).get("volume_min_ratio", min_volume))
    if latest_volume_ratio is None or latest_volume_ratio < min_volume:
        return None
    if not prior_high_breakout_ok(latest, prior, config):
        return None
    if not has_pullback_or_consolidation(bars, int(strategy["pullback_lookback_days"])):
        return None
    partial, target = setup_exit_multiples("momentum_breakout", config)
    return SetupSignal(
        setup_type="momentum_breakout",
        score=relative_strength + ((latest_volume_ratio or 1.0) * 2.0),
        reason=f"momentum breakout setup, above 50/200-day averages, +{relative_strength:.2f}% 20-day RS vs SPY",
        partial_target_r_multiple=partial,
        target_r_multiple=target,
    )


def evaluate_momentum_continuation(
    latest: Bar,
    prior: Bar,
    sma50: float,
    sma200: float,
    relative_strength: float,
    latest_volume_ratio: float | None,
    config: dict[str, Any],
) -> SetupSignal | None:
    if not setup_enabled("momentum_continuation", config):
        return None
    if not momentum_continuation_ok(latest, prior, sma50, sma200, relative_strength, latest_volume_ratio, config):
        return None
    partial, target = setup_exit_multiples("momentum_continuation", config)
    return SetupSignal(
        setup_type="momentum_continuation",
        score=relative_strength + ((latest_volume_ratio or 1.0) * 1.5),
        reason=f"momentum continuation setup, above 50/200-day averages, +{relative_strength:.2f}% 20-day RS vs SPY",
        partial_target_r_multiple=partial,
        target_r_multiple=target,
    )


def evaluate_pullback_in_uptrend(
    bars: list[Bar],
    latest: Bar,
    prior: Bar,
    closes: list[float],
    sma50: float,
    sma200: float,
    relative_strength: float,
    latest_volume_ratio: float | None,
    config: dict[str, Any],
) -> SetupSignal | None:
    if not pullback_in_uptrend_ok(bars, latest, prior, closes, sma50, sma200, relative_strength, latest_volume_ratio, config):
        return None
    partial, target = setup_exit_multiples("pullback_in_uptrend", config)
    return SetupSignal(
        setup_type="pullback_in_uptrend",
        score=max(relative_strength, 0.0) + 4.0,
        reason=f"pullback-in-uptrend setup, above 50/200-day averages, +{relative_strength:.2f}% 20-day RS vs SPY",
        partial_target_r_multiple=partial,
        target_r_multiple=target,
    )


def evaluate_sector_relative_pullback(
    bars: list[Bar],
    latest: Bar,
    prior: Bar,
    closes: list[float],
    sma200: float,
    spy_return: float,
    sector_rs: float | None,
    sector_momentum: float | None,
    latest_volume_ratio: float | None,
    config: dict[str, Any],
) -> SetupSignal | None:
    setup = config["strategy"].get("sector_relative_pullback", {})
    if not setup.get("enabled", False):
        return None
    if latest.close <= sma200:
        return None
    if sector_rs is None or sector_momentum is None:
        return None
    sector_vs_spy = sector_momentum - spy_return
    if sector_vs_spy < float(setup.get("min_sector_vs_spy_pct", 0.0)):
        return None
    if sector_rs > float(setup.get("max_stock_vs_sector_pct", -0.5)):
        return None
    if sector_rs < float(setup.get("min_stock_vs_sector_pct", -8.0)):
        return None
    rsi_value = rsi(closes, int(setup.get("rsi_days", 14)))
    if rsi_value is not None:
        if rsi_value < float(setup.get("rsi_min", 35.0)) or rsi_value > float(setup.get("rsi_max", 60.0)):
            return None
    if latest_volume_ratio is not None and latest_volume_ratio < float(setup.get("min_volume_ratio", 0.6)):
        return None
    ema_days = int(setup.get("ema_days", 20))
    moving_average = ema(closes, ema_days) or sma(closes, ema_days)
    if moving_average is None:
        return None
    if not (bullish_reversal_ok(latest, prior) or latest.close > moving_average):
        return None
    partial, target = setup_exit_multiples("sector_relative_pullback", config)
    return SetupSignal(
        setup_type="sector_relative_pullback",
        score=abs(sector_rs) + max(sector_vs_spy, 0.0) + 2.0,
        reason=f"sector-relative pullback setup, sector beat SPY by {sector_vs_spy:.2f}% while stock lagged sector by {abs(sector_rs):.2f}%",
        partial_target_r_multiple=partial,
        target_r_multiple=target,
        max_gap_pct=float(setup.get("max_gap_pct", config["strategy"]["next_session_max_gap_pct"])),
    )


def evaluate_quality_range_reversion(
    symbol: str,
    bars: list[Bar],
    latest: Bar,
    prior: Bar,
    closes: list[float],
    sma200: float,
    latest_volume_ratio: float | None,
    config: dict[str, Any],
) -> SetupSignal | None:
    setup = config["strategy"].get("quality_range_reversion", {})
    if not setup.get("enabled", False):
        return None
    allowed_groups = set(setup.get("allowed_groups", []))
    if allowed_groups and symbol_group(symbol, config) not in allowed_groups:
        return None
    if latest.close <= sma200:
        return None
    if latest.close < float(setup.get("min_price", 10.0)):
        return None
    drawdown = recent_drawdown_pct(bars, int(setup.get("lookback_days", 20)))
    if drawdown is None:
        return None
    if drawdown < float(setup.get("min_drawdown_pct", 3.0)) or drawdown > float(setup.get("max_drawdown_pct", 12.0)):
        return None
    rsi_value = rsi(closes, int(setup.get("rsi_days", 14)))
    if rsi_value is not None:
        if rsi_value < float(setup.get("rsi_min", 30.0)) or rsi_value > float(setup.get("rsi_max", 48.0)):
            return None
    if latest_volume_ratio is not None and latest_volume_ratio < float(setup.get("min_volume_ratio", 0.5)):
        return None
    if not bullish_reversal_ok(latest, prior):
        return None
    partial, target = setup_exit_multiples("quality_range_reversion", config)
    return SetupSignal(
        setup_type="quality_range_reversion",
        score=drawdown + 2.0,
        reason=f"quality range-reversion setup, {drawdown:.2f}% pullback above 200-day average with bullish reversal",
        partial_target_r_multiple=partial,
        target_r_multiple=target,
        max_gap_pct=float(setup.get("max_gap_pct", config["strategy"]["next_session_max_gap_pct"])),
    )


def choose_setup_signal(signals: list[SetupSignal], config: dict[str, Any]) -> SetupSignal | None:
    if not signals:
        return None
    return max(signals, key=lambda item: item.score * setup_weight(item.setup_type, config))


def candidate_stop(latest: Bar, bars: list[Bar], config: dict[str, Any]) -> tuple[float, str, float | None]:
    risk = config["risk"]
    swing_days = int(config["strategy"]["recent_swing_low_days"])
    recent_low = min(bar.low for bar in bars[-swing_days:])
    percent_stop = latest.close * (1.0 - float(risk["initial_stop_pct"]) / 100.0)
    legacy_stop = min(percent_stop, recent_low)

    atr_config = risk.get("atr_stop", {})
    if not atr_config.get("enabled", False):
        return legacy_stop, "percent_or_recent_low", None

    atr_value = atr(bars, int(atr_config.get("days", 14)))
    if atr_value is None:
        return legacy_stop, "percent_or_recent_low_atr_unavailable", None

    atr_stop = latest.close - (float(atr_config.get("multiple", 2.0)) * atr_value)
    mode = str(atr_config.get("mode", "tighter_of_legacy_and_atr"))
    if mode == "atr_only":
        return atr_stop, "atr", atr_value
    if mode == "wider_of_legacy_and_atr":
        return min(legacy_stop, atr_stop), "wider_of_percent_recent_low_and_atr", atr_value
    return max(legacy_stop, atr_stop), "tighter_of_percent_recent_low_and_atr", atr_value


def position_size(
    account_value: float,
    settled_cash: float,
    entry: float,
    stop: float,
    risk_per_trade_pct: float,
    max_position_pct: float,
    max_trade_risk_dollars: float | None = None,
    existing_position_value: float = 0.0,
    min_cash_reserve_pct: float = 0.0,
) -> tuple[int, float, float]:
    risk_per_share = max(entry - stop, 0.0)
    if risk_per_share <= 0:
        return 0, 0.0, 0.0

    max_risk_dollars = account_value * (risk_per_trade_pct / 100.0)
    if max_trade_risk_dollars is not None:
        max_risk_dollars = min(max_risk_dollars, max_trade_risk_dollars)
    risk_sized_shares = math.floor(max_risk_dollars / risk_per_share)
    remaining_position_cap = max(0.0, (account_value * (max_position_pct / 100.0)) - existing_position_value)
    cap_sized_shares = math.floor(remaining_position_cap / entry)
    required_cash_reserve = account_value * (min_cash_reserve_pct / 100.0)
    cash_available_for_trade = max(0.0, settled_cash - required_cash_reserve)
    cash_sized_shares = math.floor(cash_available_for_trade / entry)
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


def load_optional_json(path_or_json: str | None, default: Any) -> Any:
    if not path_or_json:
        return default
    path = Path(path_or_json)
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path_or_json)


def normalize_broker_positions(snapshot: Any) -> dict[str, dict[str, Any]]:
    if isinstance(snapshot, dict):
        if isinstance(snapshot.get("positions"), list):
            positions = snapshot["positions"]
        elif isinstance(snapshot.get("data"), dict) and isinstance(snapshot["data"].get("positions"), list):
            positions = snapshot["data"]["positions"]
        else:
            positions = []
    elif isinstance(snapshot, list):
        positions = snapshot
    else:
        positions = []
    return {
        str(position.get("symbol", "")).upper(): position
        for position in positions
        if position.get("symbol") and float(position.get("quantity", 0) or 0) > 0
    }


def normalize_broker_orders(snapshot: Any) -> list[dict[str, Any]]:
    if isinstance(snapshot, dict):
        if isinstance(snapshot.get("orders"), list):
            return snapshot["orders"]
        if isinstance(snapshot.get("data"), dict) and isinstance(snapshot["data"].get("orders"), list):
            return snapshot["data"]["orders"]
    if isinstance(snapshot, list):
        return snapshot
    return []


def open_stop_order_for_symbol(orders: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    symbol = symbol.upper()
    stops = [
        order
        for order in orders
        if str(order.get("symbol", "")).upper() == symbol
        and order.get("side") == "sell"
        and order.get("trigger") == "stop"
        and order.get("state") in {"open", "queued", "confirmed"}
    ]
    if not stops:
        return None
    return max(stops, key=lambda order: float(order.get("stop_price", 0) or 0))


def broker_position_quantity(position: dict[str, Any] | None) -> float:
    if not position:
        return 0.0
    return float(position.get("quantity", 0.0) or 0.0)


def broker_position_average_price(position: dict[str, Any] | None, fallback: float) -> float:
    if not position:
        return fallback
    for key in ("average_buy_price", "entry_price", "average_price"):
        value = position.get(key)
        if value not in (None, ""):
            return float(value)
    return fallback


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


def allow_add_to_existing_positions(config: dict[str, Any]) -> bool:
    return bool(config["strategy"].get("allow_add_to_existing_positions", False))


def option_intent_for_candidate(item: Candidate, config: dict[str, Any]) -> dict[str, Any] | None:
    options = config.get("options_strategy", {})
    if not config.get("execution", {}).get("allow_options", False) or not options.get("enabled", False):
        return None

    strategy = str(options.get("default_signal_strategy", "long_call"))
    if strategy not in set(options.get("strategies", [])):
        return None
    option_type = "call" if strategy == "long_call" else "put"
    return {
        "enabled": True,
        "strategy": strategy,
        "underlying_symbol": item.symbol,
        "underlying_type": "equity",
        "option_type": option_type,
        "position_effect": "open",
        "side": "buy",
        "min_dte": int(options.get("min_dte", 30)),
        "max_dte": int(options.get("max_dte", 60)),
        "target_delta_min": float(options.get("target_delta_min", 0.35)),
        "target_delta_max": float(options.get("target_delta_max", 0.60)),
        "max_bid_ask_spread_pct": float(options.get("max_bid_ask_spread_pct", 15.0)),
        "min_open_interest": int(options.get("min_open_interest", 100)),
        "min_volume": int(options.get("min_volume", 10)),
        "premium_risk_mode": str(options.get("premium_risk_mode", "full_premium_at_risk")),
        "max_contracts": int(options.get("max_contracts_per_trade", 1)),
        "order_type": str(options.get("order_type", "limit")),
        "time_in_force": str(options.get("time_in_force", "gfd")),
        "market_hours": str(options.get("market_hours", "regular_hours")),
        "note": "Orchestrator must resolve chain, instrument, quote, and liquidity before staging an option order.",
    }


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
    apply_news_filter: bool = True,
    sector_bars_by_symbol: dict[str, list[Bar]] | None = None,
    broker_position: dict[str, Any] | None = None,
    broker_stop_order: dict[str, Any] | None = None,
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
    existing_quantity = broker_position_quantity(broker_position)
    existing_position_value = existing_quantity * latest.close

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

    stock_return = percent_change(latest.close, closes[-rs_days - 1])
    spy_return = percent_change(spy_closes[-1], spy_closes[-rs_days - 1])
    relative_strength = stock_return - spy_return

    latest_volume_ratio = volume_ratio(bars, int(strategy.get("volume_confirmation", {}).get("lookback_days", 20)))
    min_dollar_volume = float(strategy.get("min_average_dollar_volume", 0.0) or 0.0)
    if min_dollar_volume > 0 and symbol.upper() not in ETF_SYMBOLS:
        volumes = [bar.volume or 0.0 for bar in bars]
        avg_volume = average(volumes, int(strategy.get("volume_confirmation", {}).get("lookback_days", 20)))
        if not avg_volume or avg_volume * latest.close < min_dollar_volume:
            return None
    sector_rs, sector_momentum = sector_relative_metrics(symbol, bars, sector_bars_by_symbol or {}, config)

    if existing_quantity > 0:
        add_on_config = strategy.get("add_on_controls", {})
        ema_days = int(add_on_config.get("no_chase_ema_days", 20))
        moving_average = ema(closes, ema_days) or sma(closes, ema_days)
        max_distance = float(add_on_config.get("max_distance_above_ema_pct", 3.0))
        if moving_average and latest.close > moving_average * (1.0 + max_distance / 100.0):
            return None

    same_day_move = percent_change(latest.close, prior.close)
    if same_day_move > float(strategy["same_day_chase_limit_pct"]):
        return None

    signals = [
        signal
        for signal in [
            evaluate_momentum_breakout(bars, latest, prior, sma50, sma200, relative_strength, latest_volume_ratio, config),
            evaluate_momentum_continuation(latest, prior, sma50, sma200, relative_strength, latest_volume_ratio, config),
            evaluate_pullback_in_uptrend(
                bars, latest, prior, closes, sma50, sma200, relative_strength, latest_volume_ratio, config
            ),
            evaluate_sector_relative_pullback(
                bars,
                latest,
                prior,
                closes,
                sma200,
                spy_return,
                sector_rs,
                sector_momentum,
                latest_volume_ratio,
                config,
            ),
            evaluate_quality_range_reversion(symbol, bars, latest, prior, closes, sma200, latest_volume_ratio, config),
        ]
        if signal is not None
    ]
    setup_signal = choose_setup_signal(signals, config)
    if setup_signal is None:
        return None

    stop, stop_method, atr_value = candidate_stop(latest, bars, config)
    stop_distance_pct = percent_distance(latest.close, stop)
    if stop_distance_pct < float(risk["min_stop_pct"]) or stop_distance_pct > float(risk["max_stop_pct"]):
        return None

    if apply_news_filter:
        news_ok, news_score, news_summary = normalize_news_item(symbol, news_snapshot or {}, config, latest.date)
        if not news_ok:
            return None
    else:
        news_score = 0.0
        news_summary = "news not evaluated in deterministic prescreen"

    max_gap_pct = setup_signal.max_gap_pct
    if max_gap_pct is None:
        max_gap_pct = adaptive_gap_pct(relative_strength, news_score, config)
    max_entry = max_next_session_entry(latest.close, max_gap_pct)
    shares, position_value, max_loss = position_size(
        account_value=account_value,
        settled_cash=settled_cash,
        entry=latest.close,
        stop=stop,
        risk_per_trade_pct=float(risk["risk_per_trade_pct"]),
        max_position_pct=float(risk["max_position_pct"]),
        max_trade_risk_dollars=max_trade_risk_dollars,
        existing_position_value=existing_position_value,
        min_cash_reserve_pct=float(risk.get("min_cash_reserve_pct", 0.0)),
    )
    if shares < 1:
        return None

    if existing_quantity > 0:
        add_on_config = strategy.get("add_on_controls", {})
        average_price = broker_position_average_price(broker_position, latest.close)
        existing_stop = float((broker_stop_order or {}).get("stop_price", 0.0) or broker_position.get("stop_price", 0.0) or 0.0)
        existing_risk = max(0.0, average_price - existing_stop) * existing_quantity if existing_stop > 0 else 0.0
        aggregate_symbol_risk = existing_risk + max_loss
        max_add_on_risk = account_value * (float(add_on_config.get("max_aggregate_symbol_risk_pct", 2.0)) / 100.0)
        breakeven_protected = existing_stop >= average_price if existing_stop > 0 else False
        if not breakeven_protected and aggregate_symbol_risk > max_add_on_risk:
            return None

    partial_target = r_multiple_target(latest.close, stop, setup_signal.partial_target_r_multiple)
    profit_target = r_multiple_target(latest.close, stop, setup_signal.target_r_multiple)
    reward_risk_ratio = (profit_target - latest.close) / (latest.close - stop)
    if reward_risk_ratio < float(strategy["min_reward_risk_ratio"]):
        return None
    news_weight = float(strategy.get("news_filter", {}).get("rank_weight", 0.0))
    sector_config = strategy.get("sector_relative_momentum", {})
    sector_weight = float(sector_config.get("rank_weight", 0.0)) if sector_config.get("enabled", False) else 0.0
    sector_score = sector_rs if sector_rs is not None else 0.0
    sector_momentum_score = sector_momentum - spy_return if sector_momentum is not None else 0.0
    signal_rank_score = (
        setup_signal.score * setup_weight(setup_signal.setup_type, config)
        + (relative_strength * 0.35)
        + (sector_score * sector_weight)
        + (sector_momentum_score * sector_weight * 0.5)
        + (news_score * news_weight)
    )
    usable_size_rank_score = usable_position_size_score(shares, position_value, account_value, config)
    combined_rank_score = signal_rank_score + usable_size_rank_score

    reason = setup_signal.reason
    if sector_rs is not None:
        reason = f"{reason}; stock vs sector {sector_rs:.2f}%"
    if sector_momentum is not None:
        reason = f"{reason}; sector momentum {sector_momentum:.2f}%"
    if relaxed_entry_enabled(config):
        reason = f"{reason}; relaxed entry filters active"
    if latest_volume_ratio is not None:
        reason = f"{reason}; volume {latest_volume_ratio:.2f}x 20-day average"
    if strategy.get("news_filter", {}).get("enabled", False):
        reason = f"{reason}; news score {news_score:g} ({news_summary})"
    if usable_size_rank_score:
        reason = f"{reason}; usable size rank bonus {usable_size_rank_score:.2f}"
    exit_plan = (
        f"initial {stop_method} stop {stop:.2f}; consider partial profit at "
            f"{partial_target:.2f} ({setup_signal.partial_target_r_multiple:g}R); target {profit_target:.2f} "
            f"({setup_signal.target_r_multiple:g}R); "
            f"trail remainder by {float(risk['trailing_stop_pct']):g}% or a close below the 20-day average"
    )
    return Candidate(
        symbol=symbol,
        date=latest.date,
        setup_type=setup_signal.setup_type,
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
        sector_relative_strength_pct=sector_rs,
        sector_momentum_pct=sector_momentum,
        volume_ratio=latest_volume_ratio,
        atr=atr_value,
        stop_method=stop_method,
        news_score=news_score,
        combined_rank_score=combined_rank_score,
        signal_rank_score=signal_rank_score,
        usable_size_rank_score=usable_size_rank_score,
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


def market_regime_allows_new_buys(output: dict[str, Any], prices_dir: Path, config: dict[str, Any]) -> bool:
    regime = config["strategy"].get("market_regime_filter", {})
    if not regime.get("enabled", False):
        return True

    sma_days = int(regime.get("sma_days", config["strategy"]["market_filter_sma_days"]))
    for symbol in regime.get("required_symbols_above_sma", ["SPY"]):
        path = prices_dir / f"{symbol}.csv"
        if not path.exists():
            output["messages"].append(f"New long trades blocked: market regime file missing for {symbol}.")
            return False
        bars = load_bars(path)
        moving_average = sma([bar.close for bar in bars], sma_days)
        if moving_average is None:
            output["messages"].append(f"New long trades blocked: not enough {symbol} history for market regime.")
            return False
        if bars[-1].close <= moving_average:
            output["messages"].append(
                f"New long trades blocked: {symbol} is not above its {sma_days}-day moving average."
            )
            return False
        output["messages"].append(
            f"Market regime OK: {symbol} {bars[-1].close:.2f} above {sma_days}-day SMA {moving_average:.2f}."
        )
    return True


def validate_fresh_price_history(output: dict[str, Any], prices_dir: Path, config: dict[str, Any]) -> bool:
    freshness = config.get("data_freshness", {})
    if not freshness:
        return True

    strategy = config["strategy"]
    benchmark = strategy["benchmark_symbol"]
    benchmark_path = prices_dir / f"{benchmark}.csv"
    if not benchmark_path.exists():
        output["messages"].append(f"New trades blocked: benchmark history missing for {benchmark}.")
        return False

    benchmark_bars = load_bars(benchmark_path)
    if not benchmark_bars:
        output["messages"].append(f"New trades blocked: benchmark history empty for {benchmark}.")
        return False

    benchmark_date = date.fromisoformat(benchmark_bars[-1].date)
    min_bars = int(freshness.get("min_daily_bars", 201))
    max_age = int(freshness.get("max_history_age_calendar_days", 1))
    excluded = {str(symbol).upper() for symbol in strategy.get("excluded_symbols", [])}
    invalid: list[str] = []

    for raw_symbol in strategy["trade_universe"]:
        symbol = str(raw_symbol).upper()
        if symbol in excluded:
            continue
        path = prices_dir / f"{symbol}.csv"
        if not path.exists():
            invalid.append(f"{symbol}: missing file")
            continue
        bars = load_bars(path)
        if len(bars) < min_bars:
            invalid.append(f"{symbol}: only {len(bars)} bars")
            continue
        latest_date = date.fromisoformat(bars[-1].date)
        if (benchmark_date - latest_date).days > max_age:
            invalid.append(f"{symbol}: stale latest bar {bars[-1].date}")

    if invalid:
        output["messages"].append(
            "Symbols ineligible after history refresh because data is stale or insufficient: " + "; ".join(invalid)
        )
        return True
    output["messages"].append(f"Price history freshness OK: full universe through {benchmark_bars[-1].date}.")
    return True


def revalidate_candidate(
    candidate: Candidate,
    live_price: float,
    account_value: float,
    settled_cash: float,
    config: dict[str, Any],
    max_trade_risk_dollars: float | None = None,
    broker_position: dict[str, Any] | None = None,
    broker_stop_order: dict[str, Any] | None = None,
) -> Candidate | None:
    risk = config["risk"]
    if live_price > candidate.max_entry:
        return None

    existing_quantity = broker_position_quantity(broker_position)
    existing_position_value = existing_quantity * live_price
    shares, position_value, max_loss = position_size(
        account_value=account_value,
        settled_cash=settled_cash,
        entry=live_price,
        stop=candidate.stop,
        risk_per_trade_pct=float(risk["risk_per_trade_pct"]),
        max_position_pct=float(risk["max_position_pct"]),
        max_trade_risk_dollars=max_trade_risk_dollars,
        existing_position_value=existing_position_value,
        min_cash_reserve_pct=float(risk.get("min_cash_reserve_pct", 0.0)),
    )
    if shares < 1:
        return None
    if existing_quantity > 0:
        add_on_config = config["strategy"].get("add_on_controls", {})
        average_price = broker_position_average_price(broker_position, live_price)
        existing_stop = float((broker_stop_order or {}).get("stop_price", 0.0) or broker_position.get("stop_price", 0.0) or 0.0)
        existing_risk = max(0.0, average_price - existing_stop) * existing_quantity if existing_stop > 0 else 0.0
        aggregate_symbol_risk = existing_risk + max_loss
        max_add_on_risk = account_value * (float(add_on_config.get("max_aggregate_symbol_risk_pct", 2.0)) / 100.0)
        breakeven_protected = existing_stop >= average_price if existing_stop > 0 else False
        if not breakeven_protected and aggregate_symbol_risk > max_add_on_risk:
            return None
    partial_multiple, target_multiple = setup_exit_multiples(candidate.setup_type, config)
    partial_target = r_multiple_target(live_price, candidate.stop, partial_multiple)
    profit_target = r_multiple_target(live_price, candidate.stop, target_multiple)
    reward_risk_ratio = (profit_target - live_price) / (live_price - candidate.stop)
    if reward_risk_ratio < float(config["strategy"]["min_reward_risk_ratio"]):
        return None
    signal_rank_score = candidate.combined_rank_score - candidate.usable_size_rank_score
    usable_size_rank_score = usable_position_size_score(shares, position_value, account_value, config)
    combined_rank_score = signal_rank_score + usable_size_rank_score

    exit_plan = (
        f"initial stop {candidate.stop:.2f}; consider partial profit at "
        f"{partial_target:.2f} ({partial_multiple:g}R); target {profit_target:.2f} "
        f"({target_multiple:g}R); "
        f"trail remainder by {float(risk['trailing_stop_pct']):g}% or a close below the 20-day average"
    )

    return Candidate(
        symbol=candidate.symbol,
        date=candidate.date,
        setup_type=candidate.setup_type,
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
        sector_relative_strength_pct=candidate.sector_relative_strength_pct,
        sector_momentum_pct=candidate.sector_momentum_pct,
        volume_ratio=candidate.volume_ratio,
        atr=candidate.atr,
        stop_method=candidate.stop_method,
        news_score=candidate.news_score,
        combined_rank_score=combined_rank_score,
        signal_rank_score=signal_rank_score,
        usable_size_rank_score=usable_size_rank_score,
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
        help=(
            "Comma-separated symbols or JSON array already held. Held symbols are eligible add-ons when "
            "strategy.allow_add_to_existing_positions is true."
        ),
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
    parser.add_argument(
        "--prescreen-news-symbols-only",
        action="store_true",
        help=(
            "Run deterministic technical/risk filters without applying news and emit only a ranked symbol shortlist "
            "for downstream news collection."
        ),
    )
    parser.add_argument(
        "--broker-positions-json",
        help="Optional broker positions snapshot path or JSON. Used to enforce aggregate held-symbol add-on exposure.",
    )
    parser.add_argument(
        "--broker-orders-json",
        help="Optional broker orders snapshot path or JSON. Used to find existing protective stops for add-on risk.",
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
    broker_positions = normalize_broker_positions(load_optional_json(args.broker_positions_json, []))
    broker_orders = normalize_broker_orders(load_optional_json(args.broker_orders_json, []))
    held_symbols.update(broker_positions.keys())
    open_risk_dollars = parse_float_arg(args.open_risk_dollars)
    if strategy.get("news_filter", {}).get("enabled", False):
        output["messages"].append(
            f"News filter active: latest symbol news is scored over the last "
            f"{strategy['news_filter']['lookback_hours']} hours when a news snapshot is supplied."
        )
    if args.prescreen_news_symbols_only:
        output["order_mode"] = "deterministic_prescreen_news_symbols_only"
        output["messages"].append("Token-efficient prescreen active: news is deferred until after technical/risk filters.")

    if drawdown_paused(args.account_value, args.monthly_start_equity, float(risk["drawdown_pause_pct"])):
        output["status"] = "paused"
        output["messages"].append("New trades paused: account drawdown limit reached.")
        return emit(output, args.json)

    add_vix_context(output, prices_dir, config)
    if not validate_fresh_price_history(output, prices_dir, config):
        output["status"] = "price_history_invalid"
        return emit(output, args.json)

    max_positions = int(risk["max_positions_standard"])
    if args.account_value < float(risk["funding_minimum_standard_usd"]):
        max_positions = int(risk["max_positions_under_5000"])
        output["messages"].append(f"Under $5k concentrated mode active: max positions set to {max_positions}.")

    add_to_existing_allowed = allow_add_to_existing_positions(config)
    new_position_slots = max(0, max_positions - args.positions_count)
    if args.positions_count >= max_positions and not add_to_existing_allowed:
        output["status"] = "full"
        output["messages"].append("New trades blocked: maximum open positions reached.")
        return emit(output, args.json)
    if args.positions_count >= max_positions and add_to_existing_allowed:
        output["messages"].append(
            "New symbols blocked: maximum open positions reached; add-ons to held symbols remain eligible."
        )

    total_open_risk_cap = args.account_value * (float(risk["total_open_risk_pct"]) / 100.0)
    remaining_open_risk = total_open_risk_cap - open_risk_dollars
    if remaining_open_risk <= 0:
        output["status"] = "risk_budget_full"
        output["messages"].append("New trades blocked: total open-risk budget reached.")
        return emit(output, args.json)
    output["messages"].append(
        f"Open-risk budget: ${remaining_open_risk:.2f} remaining of ${total_open_risk_cap:.2f} total cap."
    )
    min_cash_reserve = args.account_value * (float(risk.get("min_cash_reserve_pct", 0.0)) / 100.0)
    if risk.get("min_cash_reserve_pct") is not None:
        output["messages"].append(
            f"Cash reserve gate: keep at least ${min_cash_reserve:.2f} "
            f"({float(risk.get('min_cash_reserve_pct', 0.0)):g}% of account) after new buys."
        )

    if not market_regime_allows_new_buys(output, prices_dir, config):
        output["status"] = "market_filter_off"
        return emit(output, args.json)

    benchmark = strategy["benchmark_symbol"]
    spy_bars = load_bars(prices_dir / f"{benchmark}.csv")
    sector_bars_by_symbol: dict[str, list[Bar]] = {}
    sector_config = strategy.get("sector_relative_momentum", {})
    if sector_config.get("enabled", False):
        proxy_symbols = {
            str(symbol).upper()
            for symbol in sector_config.get("group_proxy_symbols", {}).values()
            if str(symbol).upper() != benchmark
        }
        for proxy_symbol in proxy_symbols:
            proxy_path = prices_dir / f"{proxy_symbol}.csv"
            if proxy_path.exists():
                sector_bars_by_symbol[proxy_symbol] = load_bars(proxy_path)

    symbols = list(strategy["trade_universe"])
    if args.account_value < float(risk["funding_minimum_standard_usd"]) and bool(risk["under_5000_prefer_etfs"]):
        symbols = [symbol for symbol in symbols if symbol in ETF_SYMBOLS]

    candidates: list[Candidate] = []
    slots = max(1, new_position_slots) if add_to_existing_allowed else new_position_slots
    held_groups = held_group_counts(held_symbols, config)
    max_group_positions = max_positions_per_group(args.account_value, config)
    for symbol in symbols:
        if symbol == benchmark:
            continue
        is_held_symbol = symbol.upper() in held_symbols
        if not is_held_symbol and new_position_slots <= 0:
            output["messages"].append(f"{symbol} skipped: maximum open positions reached for new symbols.")
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
            apply_news_filter=not args.prescreen_news_symbols_only,
            sector_bars_by_symbol=sector_bars_by_symbol,
            broker_position=broker_positions.get(symbol.upper()),
            broker_stop_order=open_stop_order_for_symbol(broker_orders, symbol),
        )
        if candidate:
            candidates.append(candidate)

    candidates.sort(key=lambda item: item.combined_rank_score, reverse=True)
    if args.prescreen_news_symbols_only:
        limit = int(config.get("token_efficiency", {}).get("prescreen_news_symbol_limit", 6))
        output["status"] = "prescreen_ready"
        output["news_collection_symbols"] = [candidate.symbol for candidate in candidates[:limit]]
        if output["news_collection_symbols"]:
            output["messages"].append(
                "Collect latest news only for prescreen survivors: "
                + ", ".join(output["news_collection_symbols"])
            )
        else:
            output["messages"].append("No symbols survived deterministic prescreen; news collection skipped.")
        return emit(output, args.json)

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
                broker_position=broker_positions.get(candidate.symbol.upper()),
                broker_stop_order=open_stop_order_for_symbol(broker_orders, candidate.symbol),
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
            "setup_type": item.setup_type,
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
            "sector_relative_strength_pct": round(item.sector_relative_strength_pct, 2)
            if item.sector_relative_strength_pct is not None
            else None,
            "sector_momentum_pct": round(item.sector_momentum_pct, 2) if item.sector_momentum_pct is not None else None,
            "volume_ratio": round(item.volume_ratio, 2) if item.volume_ratio is not None else None,
            "atr": round(item.atr, 2) if item.atr is not None else None,
            "stop_method": item.stop_method,
            "news_score": round(item.news_score, 2),
            "news_summary": item.news_summary,
            "signal_rank_score": round(item.signal_rank_score, 2),
            "usable_size_rank_score": round(item.usable_size_rank_score, 2),
            "combined_rank_score": round(item.combined_rank_score, 2),
            "exit_plan": item.exit_plan,
            "order_instruction": "Use a regular-hours limit order at or below max_next_session_entry after broker review.",
            "option_candidate": option_intent_for_candidate(item, config),
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
                "partial_target_r_multiple": setup_exit_multiples(item.setup_type, config)[0],
                "target_price": round(item.profit_target, 2),
                "target_r_multiple": setup_exit_multiples(item.setup_type, config)[1],
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
        print(f"{item['symbol']} reviewed candidate for {item['date']} ({item['setup_type']})")
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
            f"Sector RS {item['sector_relative_strength_pct'] if item['sector_relative_strength_pct'] is not None else 'n/a'}% | "
            f"Sector mom {item['sector_momentum_pct'] if item['sector_momentum_pct'] is not None else 'n/a'}% | "
            f"Volume {item['volume_ratio'] if item['volume_ratio'] is not None else 'n/a'}x | "
            f"News {item['news_score']} | Combined {item['combined_rank_score']}"
        )
        print(f"  Stop method: {item['stop_method']} | ATR: {item['atr'] if item['atr'] is not None else 'n/a'}")
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
