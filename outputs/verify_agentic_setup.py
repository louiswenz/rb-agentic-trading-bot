#!/usr/bin/env python3
"""Readiness checks for the Agentic trading automation artifacts."""

from __future__ import annotations

import json
import pathlib
import sys
import tomllib


ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
WORK = ROOT / "work"
AUTOMATIONS = pathlib.Path.home() / ".codex" / "automations"


def load_json(path: pathlib.Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_toml(path: pathlib.Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def verified_snapshot_summary(snapshot: dict) -> str:
    account = snapshot.get("account", snapshot)
    positions = snapshot.get("positions", account.get("positions", []))
    orders = snapshot.get("orders", [])
    option_positions = snapshot.get("option_positions", account.get("option_positions", []))
    option_orders = snapshot.get("option_orders", [])
    summary = {
        "account_value": account.get("account_value"),
        "buying_power": account.get("buying_power"),
        "equity": account.get("equity"),
        "agentic_allowed": account.get("agentic_allowed"),
        "option_level": account.get("option_level"),
        "positions_count": len(positions) if isinstance(positions, list) else None,
        "orders_count": len(orders) if isinstance(orders, list) else None,
        "option_positions_count": len(option_positions) if isinstance(option_positions, list) else None,
        "option_orders_count": len(option_orders) if isinstance(option_orders, list) else None,
        "snapshot_time": snapshot.get("time"),
        "snapshot_mode": snapshot.get("mode"),
        "read_only": snapshot.get("read_only"),
    }
    return json.dumps(summary, sort_keys=True)


def main() -> int:
    config_path = OUTPUTS / "strategy_config.json"
    state_path = WORK / "agentic_live_adapter_state.json"
    live_auto_path = AUTOMATIONS / "daily-agentic-account-scan" / "automation.toml"
    morning_auto_path = AUTOMATIONS / "agentic-morning-bot-run" / "automation.toml"
    codex_config_path = pathlib.Path.home() / ".codex" / "config.toml"

    config = load_json(config_path)
    state = load_json(state_path)
    live_auto = load_toml(live_auto_path)
    morning_auto = load_toml(morning_auto_path)
    codex_config = load_toml(codex_config_path)

    required_files = [
        OUTPUTS / "agentic_monitor.py",
        OUTPUTS / "agentic_task_queue.py",
        OUTPUTS / "swing_strategy.py",
        OUTPUTS / "live_trading_bot.py",
        OUTPUTS / "broker_adapters.py",
        OUTPUTS / "test_agentic_monitor.py",
        OUTPUTS / "test_swing_strategy_news.py",
        OUTPUTS / "codex_robinhood_adapter_runbook.md",
        OUTPUTS / "codex_live_orchestrator_prompt.md",
        OUTPUTS / "agentic_live_readiness_audit.md",
        OUTPUTS / "strategy_config.json",
        state_path,
    ]

    prompt = str(live_auto.get("prompt", ""))
    local_orchestrator_prompt = (OUTPUTS / "codex_live_orchestrator_prompt.md").read_text(encoding="utf-8")
    live_rrule = str(live_auto["rrule"])
    morning_prompt = str(morning_auto.get("prompt", ""))
    morning_rrule = str(morning_auto["rrule"])
    robinhood_tools = codex_config["mcp_servers"]["robinhood"]["tools"]
    verified_snapshot = state.get("last_verified_snapshot", {})
    verified_account = verified_snapshot.get("account", verified_snapshot)
    required_snapshot_fields = [
        "account_value",
        "buying_power",
        "equity_positions_count",
        "open_equity_orders_count",
    ]
    required_robinhood_approvals = [
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_orders",
        "get_equity_quotes",
        "get_index_quotes",
        "get_equity_historicals",
        "get_equity_tradability",
        "get_option_positions",
        "get_option_orders",
        "get_option_chains",
        "get_option_instruments",
        "get_option_quotes",
        "review_equity_order",
        "place_equity_order",
        "cancel_equity_order",
        "review_option_order",
        "place_option_order",
        "cancel_option_order",
    ]

    checks = [
        check(
            "required_files",
            all(path.exists() for path in required_files),
            f"{sum(path.exists() for path in required_files)}/{len(required_files)} files present",
        ),
        check(
            "auto_trading_mode",
            config["execution"]["mode"] == "auto_trade_with_notifications",
            config["execution"]["mode"],
        ),
        check(
            "max_auto_buys_per_day",
            config["execution"]["max_auto_buys_per_day"] == 2,
            str(config["execution"]["max_auto_buys_per_day"]),
        ),
        check(
            "news_filter_enabled",
            config["strategy"].get("news_filter", {}).get("enabled") is True,
            str(config["strategy"].get("news_filter", {}).get("enabled")),
        ),
        check(
            "minimum_reward_risk",
            config["strategy"].get("min_reward_risk_ratio") == 1.5,
            str(config["strategy"].get("min_reward_risk_ratio")),
        ),
        check(
            "total_open_risk_cap",
            config["risk"].get("total_open_risk_pct") == 5.0,
            str(config["risk"].get("total_open_risk_pct")),
        ),
        check(
            "risk_per_trade_cap",
            config["risk"].get("risk_per_trade_pct") == 2.0,
            str(config["risk"].get("risk_per_trade_pct")),
        ),
        check(
            "max_position_cap",
            config["risk"].get("max_position_pct") == 35.0,
            str(config["risk"].get("max_position_pct")),
        ),
        check(
            "minimum_cash_reserve",
            config["risk"].get("min_cash_reserve_pct") == 15.0,
            str(config["risk"].get("min_cash_reserve_pct")),
        ),
        check(
            "sector_concentration_disabled",
            config["strategy"].get("sector_concentration", {}).get("enabled") is False,
            str(config["strategy"].get("sector_concentration", {}).get("enabled")),
        ),
        check(
            "add_to_existing_positions_enabled",
            config["strategy"].get("allow_add_to_existing_positions") is True,
            str(config["strategy"].get("allow_add_to_existing_positions")),
        ),
        check(
            "long_options_enabled",
            config["execution"].get("allow_options") is True
            and config.get("options_strategy", {}).get("enabled") is True
            and config.get("options_strategy", {}).get("strategies") == ["long_call", "long_put"],
            str(config.get("options_strategy")),
        ),
        check(
            "automated_option_exits_enabled",
            config.get("options_exit", {}).get("enabled") is True
            and config.get("options_exit", {}).get("profit_target_pct") == 50.0
            and config.get("options_exit", {}).get("stop_loss_pct") == 35.0
            and config.get("options_exit", {}).get("min_dte_exit") == 14,
            str(config.get("options_exit")),
        ),
        check(
            "option_account_approval_gate",
            str(verified_account.get("option_level", "")) in config.get("options_strategy", {}).get(
                "require_account_option_level", []
            )
            or bool(config.get("options_strategy", {}).get("block_live_trading_without_approval", False)),
            str(verified_account.get("option_level", "")),
        ),
        check(
            "earnings_blackout_days",
            config["strategy"].get("earnings_blackout_days") == 2,
            str(config["strategy"].get("earnings_blackout_days")),
        ),
        check(
            "minimum_stock_dollar_volume",
            config["strategy"].get("min_average_dollar_volume") == 100_000_000.0,
            str(config["strategy"].get("min_average_dollar_volume")),
        ),
        check(
            "pullback_in_uptrend_enabled",
            config["strategy"].get("pullback_in_uptrend", {}).get("enabled") is True
            and float(config["strategy"].get("pullback_in_uptrend", {}).get("min_volume_ratio", 0)) > 0,
            str(config["strategy"].get("pullback_in_uptrend")),
        ),
        check(
            "sector_relative_momentum_enabled",
            config["strategy"].get("sector_relative_momentum", {}).get("enabled") is True
            and bool(config["strategy"].get("sector_relative_momentum", {}).get("group_proxy_symbols")),
            str(config["strategy"].get("sector_relative_momentum")),
        ),
        check(
            "non_momentum_setups_enabled",
            config["strategy"].get("sector_relative_pullback", {}).get("enabled") is True
            and config["strategy"].get("quality_range_reversion", {}).get("enabled") is True
            and bool(config["strategy"].get("setup_rank_weights")),
            "sector_relative_pullback and quality_range_reversion enabled",
        ),
        check(
            "r_based_profit_targets",
            config["risk"].get("partial_profit_r_multiple") == 1.0
            and config["risk"].get("synthetic_profit_target_r_multiple") == 1.5,
            f"{config['risk'].get('partial_profit_r_multiple')}R/{config['risk'].get('synthetic_profit_target_r_multiple')}R",
        ),
        check(
            "normal_monitoring_interval",
            config["monitoring"]["open_position_poll_seconds"] == 3600,
            f"{config['monitoring']['open_position_poll_seconds']} seconds",
        ),
        check(
            "elevated_monitoring_interval",
            config["monitoring"]["elevated_poll_seconds"] == 3600,
            f"{config['monitoring']['elevated_poll_seconds']} seconds",
        ),
        check(
            "market_hours_only_schedule_mode",
            config["monitoring"].get("schedule_mode") == "regular_market_hours_only",
            str(config["monitoring"].get("schedule_mode")),
        ),
        check(
            "premarket_candidate_scan_time",
            config["monitoring"].get("premarket_candidate_scan_time_pt") == "06:00",
            str(config["monitoring"].get("premarket_candidate_scan_time_pt")),
        ),
        check(
            "intraday_candidate_scan_time",
            config["monitoring"].get("intraday_candidate_scan_time_pt") == "10:00"
            and config["monitoring"].get("after_close_candidate_scan_time_pt") == "17:00"
            and config["monitoring"].get("candidate_scan_times_pt") == ["06:00", "10:00", "17:00"],
            str(config["monitoring"].get("candidate_scan_times_pt")),
        ),
        check(
            "token_efficient_candidate_prescreen",
            config.get("token_efficiency", {}).get("deterministic_prescreen_before_news") is True
            and int(config.get("token_efficiency", {}).get("prescreen_news_symbol_limit", 0)) > 0,
            str(config.get("token_efficiency")),
        ),
        check(
            "standing_authorization_state",
            state["standing_authorization"] is True,
            str(state["standing_authorization"]),
        ),
        check(
            "codex_orchestrated_live_mode",
            state["live_mode"] == "codex_orchestrated",
            state["live_mode"],
        ),
        check(
            "broker_mcp_verified",
            str(state.get("broker_mcp_status", "")).startswith("verified"),
            str(state.get("broker_mcp_status")),
        ),
        check(
            "verified_account_snapshot",
            all(isinstance(verified_account.get(field), (int, float)) for field in required_snapshot_fields)
            or (
                all(isinstance(verified_account.get(field), (int, float)) for field in ["account_value", "buying_power"])
                and isinstance(verified_snapshot.get("positions"), list)
                and isinstance(verified_snapshot.get("orders"), list)
            ),
            verified_snapshot_summary(verified_snapshot),
        ),
        check(
            "robinhood_mcp_enabled",
            codex_config["mcp_servers"]["robinhood"]["enabled"] is True,
            "enabled",
        ),
        check(
            "robinhood_required_tool_approvals",
            all(robinhood_tools.get(tool, {}).get("approval_mode") == "approve" for tool in required_robinhood_approvals),
            f"{sum(robinhood_tools.get(tool, {}).get('approval_mode') == 'approve' for tool in required_robinhood_approvals)}/{len(required_robinhood_approvals)} approvals",
        ),
        check(
            "live_automation_active",
            live_auto["status"] == "ACTIVE",
            live_auto["status"],
        ),
        check(
            "live_automation_hourly_cron",
            live_auto["kind"] == "cron"
            and "FREQ=WEEKLY" in live_rrule
            and "BYDAY=MO,TU,WE,TH,FR" in live_rrule
            and "BYHOUR=6,7,8,9,10,11,12,13,17" in live_rrule
            and "BYMINUTE=0" in live_rrule,
            live_rrule,
        ),
        check(
            "live_prompt_uses_private_state",
            "work/agentic_live_adapter_state.json" in prompt,
            "private state path referenced",
        ),
        check(
            "live_prompt_requires_robinhood_tools",
            "Robinhood" in prompt and "place" in prompt and "cancel" in prompt,
            "broker execution language present",
        ),
        check(
            "live_prompt_requires_news_filter",
            "latest stock-specific news" in prompt and "--news-json" in prompt,
            "news-aware candidate language present",
        ),
        check(
            "live_prompt_requires_task_queue",
            ("work/agentic_tasks.md" in prompt and "work/agentic_tasks.json" in prompt)
            or (
                "work/agentic_tasks.md" in local_orchestrator_prompt
                and "work/agentic_tasks.json" in local_orchestrator_prompt
            ),
            "task queue paths referenced",
        ),
        check(
            "scheduled_candidate_scan_active",
            morning_auto["status"] == "ACTIVE"
            and morning_auto["kind"] == "cron"
            and "BYHOUR=6,10,17" in morning_rrule
            and "BYMINUTE=0" in morning_rrule,
            f"{morning_auto['status']} {morning_rrule}",
        ),
        check(
            "scanner_prompt_pending_candidates_only",
            "pending candidates only" in morning_prompt and "must not review, place, cancel, or modify any order" in morning_prompt,
            "candidate scanner is non-ordering",
        ),
    ]

    passed = all(item["passed"] for item in checks)
    print(json.dumps({"passed": passed, "checks": checks}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
