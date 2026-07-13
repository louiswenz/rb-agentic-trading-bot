#!/usr/bin/env python3
"""Narrow local-state updater for the Agentic Robinhood adapter.

This helper exists so heartbeat runs can persist local state through a
repo-scoped command instead of broad inline ``python3 -c`` snippets.
It only edits the JSON state file passed with ``--state``.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_STATE_PATH = Path("work/agentic_live_adapter_state.json")


def load_json_arg(value: str | None, default: Any) -> Any:
    if value is None:
        return deepcopy(default)
    stripped = value.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(value)
    path = Path(value)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def parse_nullish(value: str | None) -> str | None:
    if value is None:
        return None
    if value.lower() in {"", "none", "null"}:
        return None
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update Agentic local adapter state with narrow, explicit fields.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Path to Agentic state JSON.")
    parser.add_argument("--last-live-monitor-json", help="JSON object or path for state.last_live_monitor.")
    parser.add_argument("--last-candidate-scan-json", help="JSON object or path for state.last_candidate_scan.")
    parser.add_argument("--pending-candidates-json", help="JSON array or path for state.pending_candidates.")
    parser.add_argument("--broker-mcp-status", help="Value for state.broker_mcp_status.")
    parser.add_argument("--broker-mcp-verified-at", help="Value for state.broker_mcp_verified_at.")
    parser.add_argument("--status", help="Value for state.status.")
    parser.add_argument("--paused-reason", help="Value for state.paused_reason. Use null/none/empty to clear.")
    parser.add_argument("--clear-paused-reason", action="store_true", help="Set state.paused_reason to null.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print summary without writing state.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    state_path = Path(args.state)
    state = load_state(state_path)
    updated_fields: list[str] = []

    if args.last_live_monitor_json:
        value = load_json_arg(args.last_live_monitor_json, {})
        if not isinstance(value, dict):
            parser.error("--last-live-monitor-json must resolve to a JSON object")
        state["last_live_monitor"] = value
        updated_fields.append("last_live_monitor")

    if args.last_candidate_scan_json:
        value = load_json_arg(args.last_candidate_scan_json, {})
        if not isinstance(value, dict):
            parser.error("--last-candidate-scan-json must resolve to a JSON object")
        state["last_candidate_scan"] = value
        updated_fields.append("last_candidate_scan")

    if args.pending_candidates_json:
        value = load_json_arg(args.pending_candidates_json, [])
        if not isinstance(value, list):
            parser.error("--pending-candidates-json must resolve to a JSON array")
        state["pending_candidates"] = value
        updated_fields.append("pending_candidates")

    if args.broker_mcp_status is not None:
        state["broker_mcp_status"] = args.broker_mcp_status
        updated_fields.append("broker_mcp_status")

    if args.broker_mcp_verified_at is not None:
        state["broker_mcp_verified_at"] = args.broker_mcp_verified_at
        updated_fields.append("broker_mcp_verified_at")

    if args.status is not None:
        state["status"] = args.status
        updated_fields.append("status")

    if args.clear_paused_reason:
        state["paused_reason"] = None
        updated_fields.append("paused_reason")
    elif args.paused_reason is not None:
        state["paused_reason"] = parse_nullish(args.paused_reason)
        updated_fields.append("paused_reason")

    if not updated_fields:
        parser.error("No update arguments supplied")

    if not args.dry_run:
        save_state(state_path, state)

    live_monitor = state.get("last_live_monitor") or {}
    candidate_scan = state.get("last_candidate_scan") or {}
    summary = {
        "updated": not args.dry_run,
        "dry_run": args.dry_run,
        "state": str(state_path),
        "updated_fields": updated_fields,
        "status": state.get("status"),
        "paused_reason": state.get("paused_reason"),
        "last_live_monitor_status": live_monitor.get("status"),
        "last_candidate_scan_status": candidate_scan.get("status"),
        "pending_candidates_count": len(state.get("pending_candidates") or []),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
