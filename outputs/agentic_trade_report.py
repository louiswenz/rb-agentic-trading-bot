#!/usr/bin/env python3
"""Summarize the Agentic trade ledger without exposing broker account data."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [record for record in records if record.get("record_type") == "exit_event"]
    realized = [record for record in records if record.get("realized_pnl") is not None]
    wins = [record for record in realized if float(record["realized_pnl"]) > 0]
    losses = [record for record in realized if float(record["realized_pnl"]) < 0]
    by_setup: dict[str, list[float]] = defaultdict(list)
    losses_by_cause: dict[str, int] = defaultdict(int)
    for record in realized:
        setup = str(record.get("setup_type") or "unknown")
        by_setup[setup].append(float(record["realized_pnl"]))
        if float(record["realized_pnl"]) < 0:
            losses_by_cause[str(record.get("exit_reason") or "unknown")] += 1
    return {
        "records": len(records),
        "planned_candidates": sum(1 for record in records if record.get("record_type") == "candidate_planned"),
        "candidate_rejections": sum(1 for record in records if record.get("record_type") == "candidate_rejected"),
        "closed_events": len(closed),
        "realized_trades": len(realized),
        "win_rate": round(len(wins) / len(realized), 4) if realized else None,
        "average_win": round(sum(float(record["realized_pnl"]) for record in wins) / len(wins), 2) if wins else None,
        "average_loss": round(sum(float(record["realized_pnl"]) for record in losses) / len(losses), 2)
        if losses
        else None,
        "expectancy_by_setup": {
            setup: round(sum(values) / len(values), 2)
            for setup, values in sorted(by_setup.items())
            if values
        },
        "losses_by_cause": dict(sorted(losses_by_cause.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize work/agentic_trade_ledger.jsonl.")
    parser.add_argument("--ledger-jsonl", default="work/agentic_trade_ledger.jsonl")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    summary = summarize(load_records(Path(args.ledger_jsonl)))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    print(f"Records: {summary['records']}")
    print(f"Planned candidates: {summary['planned_candidates']}")
    print(f"Candidate rejections: {summary['candidate_rejections']}")
    print(f"Closed events: {summary['closed_events']}")
    print(f"Realized trades: {summary['realized_trades']}")
    print(f"Win rate: {summary['win_rate'] if summary['win_rate'] is not None else 'n/a'}")
    print(f"Average win: {summary['average_win'] if summary['average_win'] is not None else 'n/a'}")
    print(f"Average loss: {summary['average_loss'] if summary['average_loss'] is not None else 'n/a'}")
    print(f"Expectancy by setup: {summary['expectancy_by_setup'] or 'n/a'}")
    print(f"Losses by cause: {summary['losses_by_cause'] or 'n/a'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
