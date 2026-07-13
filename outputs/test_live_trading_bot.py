#!/usr/bin/env python3
"""Tests for the Agentic live-bot runner."""

from __future__ import annotations

import pathlib
import sys
import unittest


OUTPUTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(OUTPUTS))

import live_trading_bot  # noqa: E402


class LiveTradingBotTests(unittest.TestCase):
    def test_console_output_redacts_broker_identifiers(self) -> None:
        payload = {
            "state": {
                "agentic_account_number": "123456789",
                "last_equity_order_ids": ["order-1", "order-2"],
                "positions": [
                    {
                        "symbol": "RTX",
                        "protective_stop_order_id": "stop-1",
                    }
                ],
            },
            "actions": [
                {
                    "type": "cancel_order",
                    "order_id": "order-3",
                    "cancel_existing_order_ids": ["order-4"],
                }
            ],
        }

        redacted = live_trading_bot.sanitize_for_output(payload)

        self.assertEqual(redacted["state"]["agentic_account_number"], "[REDACTED]")
        self.assertEqual(redacted["state"]["last_equity_order_ids"], "[REDACTED_LIST:2]")
        self.assertEqual(redacted["state"]["positions"][0]["protective_stop_order_id"], "[REDACTED]")
        self.assertEqual(redacted["actions"][0]["order_id"], "[REDACTED]")
        self.assertEqual(redacted["actions"][0]["cancel_existing_order_ids"], "[REDACTED_LIST:1]")
        self.assertEqual(redacted["state"]["positions"][0]["symbol"], "RTX")


if __name__ == "__main__":
    unittest.main()
